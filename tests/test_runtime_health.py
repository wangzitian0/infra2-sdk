import asyncio
import json
import time
from contextvars import ContextVar

import pytest

from infra2_sdk.runtime.environment import EnvironmentTier
from infra2_sdk.runtime.health import (
    Dependency,
    DependencyKind,
    DependencyManifest,
    DependencyStatus,
    DependencyUnavailableError,
    HealthStatus,
    ProbeResult,
    _run_sync_probe,
    assert_required_dependencies,
    check_health,
    health_response,
    run_probes,
)


class Check:
    def __init__(self, name: str, *, present: bool = True, detail: str = "", error=None) -> None:
        self.name = name
        self.present = present
        self.detail = detail
        self.error = error

    def probe(self) -> ProbeResult:
        if self.error is not None:
            raise self.error
        status = DependencyStatus.PRESENT if self.present else DependencyStatus.ABSENT
        return ProbeResult(self.name, status, self.detail, 1.5)


class HangingCheck:
    name = "hanging"

    async def probe(self) -> ProbeResult:
        await asyncio.sleep(5)
        return ProbeResult(self.name, DependencyStatus.PRESENT)


def manifest() -> DependencyManifest:
    return DependencyManifest(
        [
            Dependency(
                "database",
                DependencyKind.CODE_DOMINANT,
                frozenset({EnvironmentTier.STAGING, EnvironmentTier.PRODUCTION}),
                frozenset({"DATABASE_URL"}),
            ),
            Dependency(
                "cache",
                DependencyKind.CODE_DOMINANT,
                frozenset({EnvironmentTier.PRODUCTION}),
                frozenset({"CACHE_URL"}),
            ),
        ]
    )


async def test_all_present_is_healthy_200_with_a_stable_json_body() -> None:
    code, body = await check_health([Check("database", detail="ok"), Check("cache")])
    assert code == 200
    assert body == {
        "status": "healthy",
        "checks": {
            "cache": {"status": "present", "detail": "", "duration_ms": 1.5, "required": True},
            "database": {"status": "present", "detail": "ok", "duration_ms": 1.5, "required": True},
        },
        "reasons": [],
    }
    assert json.loads(json.dumps(body)) == body


async def test_a_required_dependency_down_is_503_with_the_reason() -> None:
    code, body = await check_health(
        [Check("database", present=False, detail="connection refused"), Check("cache")],
    )
    assert code == 503
    assert body["status"] == HealthStatus.UNHEALTHY == "unhealthy"
    assert body["checks"]["database"]["status"] == "absent"
    assert body["checks"]["database"]["required"] is True
    assert body["reasons"] == ["database: connection refused"]


async def test_a_raising_probe_is_a_503_naming_the_exception_never_a_200() -> None:
    code, body = await check_health(
        [Check("database", error=RuntimeError("boom")), Check("cache")],
    )
    assert code == 503
    assert body["status"] == "unhealthy"
    assert body["checks"]["database"]["status"] == "absent"
    assert body["reasons"] == ["database: RuntimeError: boom"]


async def test_a_probe_that_hangs_past_the_timeout_is_a_503() -> None:
    code, body = await check_health([HangingCheck()], timeout_seconds=0.01)
    assert code == 503
    assert "timed out" in body["checks"]["hanging"]["detail"]


async def test_only_an_optional_dependency_down_is_degraded_but_still_200() -> None:
    code, body = await check_health(
        [Check("database"), Check("cache", present=False, detail="evicted")],
        required={"database"},
    )
    assert code == 200
    assert body["status"] == "degraded"
    assert body["checks"]["cache"]["required"] is False
    assert body["checks"]["database"]["required"] is True
    assert body["reasons"] == ["cache: evicted"]


async def test_an_optional_probe_that_raises_degrades_instead_of_failing_the_service() -> None:
    code, body = await check_health(
        [Check("database"), Check("cache", error=OSError("down"))],
        required=["database"],
    )
    assert (code, body["status"]) == (200, "degraded")
    assert body["reasons"] == ["cache: OSError: down"]


async def test_a_required_dependency_that_was_never_probed_is_absent_not_ignored() -> None:
    code, body = await check_health([Check("database")], required={"database", "cache"})
    assert code == 503
    assert body["checks"]["cache"] == {
        "status": "absent",
        "detail": "required dependency was not probed",
        "duration_ms": 0.0,
        "required": True,
    }
    assert body["reasons"] == ["cache: required dependency was not probed"]


async def test_required_failures_are_listed_before_optional_ones() -> None:
    _, body = await check_health(
        [
            Check("a-optional", present=False, detail="x"),
            Check("z-required", present=False, detail="y"),
        ],
        required={"z-required"},
    )
    assert body["reasons"] == ["z-required: y", "a-optional: x"]


@pytest.mark.parametrize(
    "tier,expected",
    [
        (EnvironmentTier.LOCAL_DEV, (200, "degraded")),
        (EnvironmentTier.STAGING, (503, "unhealthy")),
        ("production", (503, "unhealthy")),
    ],
)
async def test_manifest_and_tier_decide_which_dependencies_are_required(tier, expected) -> None:
    code, body = await check_health(
        [Check("database", present=False, detail="refused"), Check("cache")],
        manifest=manifest(),
        tier=tier,
    )
    assert (code, body["status"]) == expected


async def test_manifest_requirements_missing_from_the_checks_fail_the_report() -> None:
    code, body = await check_health([Check("database")], manifest=manifest(), tier="production")
    assert code == 503
    assert body["reasons"] == ["cache: required dependency was not probed"]


async def test_a_manifest_without_a_matching_tier_requirement_does_not_invent_one() -> None:
    code, body = await check_health([Check("database")], manifest=manifest(), tier="local_dev")
    assert (code, body["status"]) == (200, "healthy")
    assert body["checks"]["database"]["required"] is False


async def test_every_check_is_required_by_default() -> None:
    code, body = await check_health([Check("database"), Check("cache", present=False)])
    assert (code, body["status"]) == (503, "unhealthy")


async def test_misuse_raises_instead_of_reporting_a_green() -> None:
    with pytest.raises(ValueError, match="nothing to report on"):
        await check_health([])
    with pytest.raises(ValueError, match="either required or manifest"):
        await check_health([Check("database")], required={"database"}, manifest=manifest())
    with pytest.raises(ValueError, match="tier is required"):
        await check_health([Check("database")], manifest=manifest())
    with pytest.raises(ValueError, match="unknown environment"):
        await check_health([Check("database")], manifest=manifest(), tier="typo")
    with pytest.raises(ValueError, match="duplicate probe name"):
        await check_health([Check("database"), Check("database")])
    with pytest.raises(ValueError, match="timeout_seconds"):
        await check_health([Check("database")], timeout_seconds=0)


def test_health_response_summarizes_finished_probe_results_synchronously() -> None:
    present = ProbeResult("database", DependencyStatus.PRESENT, "ok", 2.0)
    absent = ProbeResult("cache", DependencyStatus.ABSENT, "refused", 3.0)
    assert health_response([present], required={"database"})[0] == 200
    code, body = health_response([present, absent], required={"database", "cache"})
    assert (code, body["status"], body["reasons"]) == (503, "unhealthy", ["cache: refused"])
    with pytest.raises(ValueError, match="duplicate probe result name"):
        health_response([present, present], required=set())
    with pytest.raises(ValueError, match="nothing to report on"):
        health_response([], required=[])


def test_an_absent_result_without_detail_still_explains_itself() -> None:
    _, body = health_response([ProbeResult("database", DependencyStatus.ABSENT)], required=set())
    assert body["reasons"] == ["database: absent"]


# --- Dependency & DependencyManifest tests ---


def _sample_dependency(name: str = "database") -> Dependency:
    return Dependency(
        name=name,
        kind=DependencyKind.CODE_DOMINANT,
        required_in=frozenset({EnvironmentTier.STAGING, EnvironmentTier.PRODUCTION}),
        env_vars=frozenset({"DATABASE_URL"}),
        summary="Postgres",
        local_backend="postgres",
        deployed_backend="postgres",
    )


def test_dependency_manifest_round_trip_and_tier_lookup() -> None:
    original = DependencyManifest((_sample_dependency(),))
    restored = DependencyManifest.from_dict(original.to_dict())
    assert restored.to_dict() == original.to_dict()
    assert restored.names() == frozenset({"database"})
    assert restored.get("database").summary == "Postgres"
    assert restored.required_for("staging") == frozenset({"database"})
    assert restored.required_for("preview") == frozenset()
    assert len(restored) == 1
    schema = DependencyManifest.json_schema()
    assert schema["$schema"].endswith("2020-12/schema")
    assert schema["properties"]["contract_version"] == {"const": 1}
    normalized = Dependency(
        "cache",
        "code_dominant",
        frozenset({"staging"}),
        frozenset({"REDIS_URL"}),
    )
    assert normalized.kind is DependencyKind.CODE_DOMINANT
    assert normalized.required_in == frozenset({EnvironmentTier.STAGING})


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"name": "bad/name"}, "identifier"),
        ({"required_in": frozenset()}, "at least one"),
        ({"env_vars": frozenset()}, "environment variables"),
        ({"env_vars": frozenset({"lower"})}, "uppercase"),
    ],
)
def test_dependency_validation(changes, message) -> None:
    values = _sample_dependency().__dict__ | changes
    with pytest.raises(ValueError, match=message):
        Dependency(**values)


def test_manifest_rejects_duplicates_and_bad_wire_values() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        DependencyManifest((_sample_dependency(), _sample_dependency()))
    with pytest.raises(ValueError, match="array"):
        DependencyManifest.from_dict({"contract_version": 1, "dependencies": "bad"})
    with pytest.raises(ValueError, match="object"):
        DependencyManifest.from_dict({"contract_version": 1, "dependencies": ["bad"]})
    with pytest.raises(ValueError, match="required_in"):
        Dependency.from_dict({"name": "db", "kind": "code_dominant"})
    raw = _sample_dependency().to_dict()
    raw["env_vars"] = [1]
    with pytest.raises(ValueError, match="contain strings"):
        Dependency.from_dict(raw)
    with pytest.raises(ValueError, match="unsupported"):
        DependencyManifest((), contract_version=2)
    with pytest.raises(ValueError, match="integer"):
        DependencyManifest((), contract_version=True)
    with pytest.raises(ValueError, match="integer"):
        DependencyManifest.from_dict({"contract_version": True, "dependencies": []})


# --- Probes & Runner tests ---


class _ProbeCheck:
    def __init__(self, name, result=None, error=None) -> None:
        self.name = name
        self.result = result
        self.error = error

    def probe(self):
        if self.error:
            raise self.error
        return self.result


class _SlowCheck:
    name = "slow"

    async def probe(self):
        await asyncio.sleep(0.05)
        return ProbeResult(self.name, DependencyStatus.PRESENT)


class _SlowSyncCheck:
    name = "slow-sync"

    def probe(self):
        time.sleep(0.25)
        return ProbeResult(self.name, DependencyStatus.PRESENT)


class _WrappedAsyncCheck:
    name = "wrapped"

    def probe(self):
        async def inner():
            return ProbeResult(self.name, DependencyStatus.PRESENT)

        return inner()


def _single_dependency_manifest() -> DependencyManifest:
    return DependencyManifest(
        (
            Dependency(
                "database",
                DependencyKind.CODE_DOMINANT,
                frozenset({EnvironmentTier.STAGING}),
                frozenset({"DATABASE_URL"}),
            ),
        )
    )


async def test_runner_supports_sync_async_errors_and_timeouts() -> None:
    present = ProbeResult("database", DependencyStatus.PRESENT, "ok", 1)
    results = await run_probes(
        (
            _ProbeCheck("database", present),
            _ProbeCheck("broken", error=OSError("down")),
            _SlowCheck(),
            _WrappedAsyncCheck(),
        ),
        timeout_seconds=0.01,
    )
    assert results[0] is present
    assert "OSError: down" in results[1].detail
    assert results[2].status is DependencyStatus.ABSENT
    assert "timed out" in results[2].detail
    assert results[3].present


def test_sync_probe_timeout_does_not_delay_cli_event_loop_shutdown() -> None:
    started = time.perf_counter()
    results = asyncio.run(run_probes((_SlowSyncCheck(),), timeout_seconds=0.01))
    elapsed = time.perf_counter() - started
    assert results[0].status is DependencyStatus.ABSENT
    assert "timed out" in results[0].detail
    assert elapsed < 0.1


async def test_sync_probe_preserves_caller_context() -> None:
    coordinate = ContextVar("coordinate", default="missing")
    coordinate.set("staging")

    class ContextCheck:
        name = "context"

        def probe(self):
            return ProbeResult(self.name, DependencyStatus.PRESENT, coordinate.get())

    result = await run_probes((ContextCheck(),))
    assert result[0].detail == "staging"


def test_probe_wire_round_trip_and_required_gate() -> None:
    result = ProbeResult("database", DependencyStatus.PRESENT, "ok", 1.5)
    assert ProbeResult("database", "present").present
    assert ProbeResult.from_dict(result.to_dict()) == result
    assert result.json_schema()["properties"]["status"]["enum"] == ["present", "absent"]
    assert_required_dependencies(_single_dependency_manifest(), "staging", (result,))
    with pytest.raises(DependencyUnavailableError) as exc:
        assert_required_dependencies(_single_dependency_manifest(), "staging", ())
    assert exc.value.missing == ("database",)


def test_probe_validation_rejects_ambiguous_results() -> None:
    with pytest.raises(ValueError, match="name"):
        ProbeResult("", DependencyStatus.PRESENT)
    with pytest.raises(ValueError, match="non-negative"):
        ProbeResult("db", DependencyStatus.PRESENT, duration_ms=-1)
    with pytest.raises(ValueError, match="numeric"):
        ProbeResult.from_dict({"name": "db", "status": "present", "duration_ms": "bad"})
    duplicate = ProbeResult("database", DependencyStatus.PRESENT)
    with pytest.raises(ValueError, match="duplicate"):
        assert_required_dependencies(
            _single_dependency_manifest(), "staging", (duplicate, duplicate)
        )


async def test_runner_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="positive"):
        await run_probes((), timeout_seconds=0)
    with pytest.raises(ValueError, match="duplicate"):
        await run_probes((_ProbeCheck("x"), _ProbeCheck("x")))
    result = await run_probes((_ProbeCheck("x", ProbeResult("other", DependencyStatus.PRESENT)),))
    assert result[0].status is DependencyStatus.ABSENT
    assert "does not match" in result[0].detail


async def test_sync_probe_propagates_system_exit() -> None:
    def exit_probe():
        raise SystemExit(42)

    with pytest.raises(SystemExit) as exc_info:
        await _run_sync_probe(exit_probe, name="exit")
    assert exc_info.value.code == 42
