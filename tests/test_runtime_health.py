import asyncio
import json

import pytest

from infra2_sdk.runtime import HealthStatus, check_health, health_response
from infra2_sdk.runtime.dependencies import Dependency, DependencyKind, DependencyManifest
from infra2_sdk.runtime.environment import EnvironmentTier
from infra2_sdk.runtime.probes import DependencyStatus, ProbeResult


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
