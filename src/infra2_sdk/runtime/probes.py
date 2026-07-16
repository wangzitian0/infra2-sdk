"""Dependency probe values, runners, and manifest enforcement."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Iterable, Mapping
from contextlib import suppress
from contextvars import copy_context
from dataclasses import asdict, dataclass
from enum import StrEnum
from functools import partial
from threading import Thread
from typing import Any, Protocol, runtime_checkable

from infra2_sdk.runtime.dependencies import DependencyManifest
from infra2_sdk.runtime.environment import EnvironmentTier

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


class DependencyStatus(StrEnum):
    PRESENT = "present"
    ABSENT = "absent"


@dataclass(frozen=True)
class ProbeResult:
    name: str
    status: DependencyStatus
    detail: str = ""
    duration_ms: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", DependencyStatus(self.status))
        if not self.name:
            raise ValueError("probe name is required")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")

    @property
    def present(self) -> bool:
        return self.status is DependencyStatus.PRESENT

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ProbeResult:
        try:
            duration_ms = float(raw.get("duration_ms", 0.0))
        except (TypeError, ValueError):
            raise ValueError("duration_ms must be numeric") from None
        return cls(
            name=_string(raw, "name"),
            status=DependencyStatus(_string(raw, "status")),
            detail=_string(raw, "detail", required=False),
            duration_ms=duration_ms,
        )

    @staticmethod
    def json_schema() -> dict[str, Any]:
        return {
            "$schema": JSON_SCHEMA_DIALECT,
            "title": "Runtime dependency probe result",
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "status", "detail", "duration_ms"],
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "status": {"enum": [status.value for status in DependencyStatus]},
                "detail": {"type": "string"},
                "duration_ms": {"type": "number", "minimum": 0},
            },
        }


@runtime_checkable
class DependencyCheck(Protocol):
    name: str

    def probe(self) -> ProbeResult | Awaitable[ProbeResult]: ...


class DependencyUnavailableError(RuntimeError):
    def __init__(self, missing: Iterable[str]) -> None:
        self.missing = tuple(sorted(set(missing)))
        super().__init__(f"required runtime dependencies are absent: {', '.join(self.missing)}")


async def run_probes(
    checks: Iterable[DependencyCheck],
    *,
    timeout_seconds: float = 10.0,
) -> tuple[ProbeResult, ...]:
    """Run sync or async checks concurrently and report ordinary failures as absent."""

    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    values = tuple(checks)
    names = [check.name for check in values]
    if len(names) != len(set(names)):
        raise ValueError("duplicate probe name")

    async def execute(check: DependencyCheck) -> ProbeResult:
        started = time.perf_counter()
        try:
            if inspect.iscoroutinefunction(check.probe):
                result = await check.probe()
            else:
                result = await _run_sync_probe(check.probe, name=check.name)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, ProbeResult):
                raise TypeError("probe must return ProbeResult")
            if result.name != check.name:
                raise ValueError("probe result name does not match check name")
            return result
        except Exception as exc:  # noqa: BLE001 - a probe converts outages to evidence
            return ProbeResult(
                check.name,
                DependencyStatus.ABSENT,
                f"{type(exc).__name__}: {exc}",
                _elapsed(started),
            )

    async def bounded(check: DependencyCheck) -> ProbeResult:
        try:
            return await asyncio.wait_for(execute(check), timeout=timeout_seconds)
        except TimeoutError:
            return ProbeResult(
                check.name,
                DependencyStatus.ABSENT,
                f"probe timed out after {timeout_seconds:g}s",
                timeout_seconds * 1000,
            )

    return tuple(await asyncio.gather(*(bounded(check) for check in values)))


async def _run_sync_probe(probe, *, name: str):
    """Run a sync probe without making CLI shutdown wait for timed-out worker threads."""

    loop = asyncio.get_running_loop()
    future = loop.create_future()
    context = copy_context()

    def publish(*, result=None, error: Exception | None = None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def worker() -> None:
        try:
            result = context.run(probe)
        except Exception as exc:  # noqa: BLE001 - forwarded to the async runner
            callback = partial(publish, error=exc)
        except BaseException as exc:  # pragma: no cover - defensive thread boundary
            error = RuntimeError(f"sync probe aborted with {type(exc).__name__}")
            callback = partial(publish, error=error)
        else:
            callback = partial(publish, result=result)
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(callback)

    Thread(target=worker, name=f"infra2-probe-{name}", daemon=True).start()
    return await future


def assert_required_dependencies(
    manifest: DependencyManifest,
    tier: str | EnvironmentTier,
    results: Iterable[ProbeResult],
) -> None:
    """Fail when required checks are absent, missing, or duplicated."""

    values = tuple(results)
    names = [result.name for result in values]
    if len(names) != len(set(names)):
        raise ValueError("duplicate probe result name")
    by_name = {result.name: result for result in values}
    missing = [
        name
        for name in manifest.required_for(tier)
        if name not in by_name or not by_name[name].present
    ]
    if missing:
        raise DependencyUnavailableError(missing)


def _elapsed(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _string(raw: Mapping[str, Any], key: str, *, required: bool = True) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key} is required")
    return value
