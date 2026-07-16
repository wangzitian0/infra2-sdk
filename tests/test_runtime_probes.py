import asyncio
import time
from contextvars import ContextVar

import pytest

from infra2_sdk.runtime.dependencies import Dependency, DependencyKind, DependencyManifest
from infra2_sdk.runtime.environment import EnvironmentTier
from infra2_sdk.runtime.probes import (
    DependencyStatus,
    DependencyUnavailableError,
    ProbeResult,
    assert_required_dependencies,
    run_probes,
)


class Check:
    def __init__(self, name, result=None, error=None) -> None:
        self.name = name
        self.result = result
        self.error = error

    def probe(self):
        if self.error:
            raise self.error
        return self.result


class SlowCheck:
    name = "slow"

    async def probe(self):
        await asyncio.sleep(0.05)
        return ProbeResult(self.name, DependencyStatus.PRESENT)


class SlowSyncCheck:
    name = "slow-sync"

    def probe(self):
        time.sleep(0.25)
        return ProbeResult(self.name, DependencyStatus.PRESENT)


class WrappedAsyncCheck:
    name = "wrapped"

    def probe(self):
        async def inner():
            return ProbeResult(self.name, DependencyStatus.PRESENT)

        return inner()


def manifest() -> DependencyManifest:
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
            Check("database", present),
            Check("broken", error=OSError("down")),
            SlowCheck(),
            WrappedAsyncCheck(),
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
    results = asyncio.run(run_probes((SlowSyncCheck(),), timeout_seconds=0.01))
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
    assert_required_dependencies(manifest(), "staging", (result,))
    with pytest.raises(DependencyUnavailableError) as exc:
        assert_required_dependencies(manifest(), "staging", ())
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
        assert_required_dependencies(manifest(), "staging", (duplicate, duplicate))


async def test_runner_rejects_bad_input() -> None:
    with pytest.raises(ValueError, match="positive"):
        await run_probes((), timeout_seconds=0)
    with pytest.raises(ValueError, match="duplicate"):
        await run_probes((Check("x"), Check("x")))
    result = await run_probes((Check("x", ProbeResult("other", DependencyStatus.PRESENT)),))
    assert result[0].status is DependencyStatus.ABSENT
    assert "does not match" in result[0].detail
