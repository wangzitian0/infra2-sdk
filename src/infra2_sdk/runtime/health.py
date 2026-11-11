"""Framework-agnostic readiness: dependency manifests, probes, and health check runner."""

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

from infra2_sdk._wire import parse_contract_version, parse_string, require_contract_version
from infra2_sdk.runtime.environment import EnvironmentTier, resolve_environment_tier

_DEPENDENCY_MANIFEST_VERSION = 1
_HTTP_OK, _HTTP_SERVICE_UNAVAILABLE = 200, 503


class DependencyKind(StrEnum):
    CODE_DOMINANT = "code_dominant"
    MODEL_DOMINANT = "model_dominant"
    ORCHESTRATOR = "orchestrator"


@dataclass(frozen=True)
class Dependency:
    name: str
    kind: DependencyKind
    required_in: frozenset[EnvironmentTier]
    env_vars: frozenset[str]
    summary: str = ""
    local_backend: str = ""
    deployed_backend: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", DependencyKind(self.kind))
        object.__setattr__(
            self, "required_in", frozenset(resolve_environment_tier(t) for t in self.required_in)
        )
        object.__setattr__(self, "env_vars", frozenset(self.env_vars))
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("dependency name must be a non-empty identifier")
        if not self.required_in:
            raise ValueError(f"dependency {self.name!r} must be required in at least one tier")
        if not self.env_vars:
            raise ValueError(f"dependency {self.name!r} must declare environment variables")
        if any(not k or k.upper() != k for k in self.env_vars):
            raise ValueError("dependency environment variables must be uppercase")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "required_in": sorted(t.value for t in self.required_in),
            "env_vars": sorted(self.env_vars),
            "summary": self.summary,
            "local_backend": self.local_backend,
            "deployed_backend": self.deployed_backend,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Dependency:
        req, ev = raw.get("required_in"), raw.get("env_vars")
        if not isinstance(req, list) or not isinstance(ev, list):
            raise ValueError("required_in and env_vars must be arrays")
        if any(not isinstance(v, str) for v in (*req, *ev)):
            raise ValueError("required_in and env_vars must contain strings")
        return cls(
            name=parse_string(raw, "name"),
            kind=DependencyKind(parse_string(raw, "kind")),
            required_in=frozenset(resolve_environment_tier(v) for v in req),
            env_vars=frozenset(ev),
            summary=parse_string(raw, "summary", required=False),
            local_backend=parse_string(raw, "local_backend", required=False),
            deployed_backend=parse_string(raw, "deployed_backend", required=False),
        )


class DependencyManifest:
    def __init__(
        self,
        dependencies: Iterable[Dependency],
        *,
        contract_version: int = _DEPENDENCY_MANIFEST_VERSION,
    ) -> None:
        require_contract_version(
            contract_version,
            _DEPENDENCY_MANIFEST_VERSION,
            description="dependency manifest version",
        )
        self.contract_version = contract_version
        vals = tuple(dependencies)
        if len(vals) != len({d.name for d in vals}):
            raise ValueError("duplicate dependency name")
        self._dependencies = vals
        self._by_name = {d.name: d for d in vals}

    def __iter__(self):
        return iter(self._dependencies)

    def __len__(self) -> int:
        return len(self._dependencies)

    def __contains__(self, name: object) -> bool:
        return name in self._by_name

    def __getitem__(self, name: str) -> Dependency:
        return self._by_name[name]

    def get(self, name: str) -> Dependency | None:
        return self._by_name.get(name)

    def names(self) -> frozenset[str]:
        return frozenset(self._by_name)

    def required_for(self, tier: str | EnvironmentTier) -> frozenset[str]:
        t = resolve_environment_tier(tier)
        return frozenset(d.name for d in self._dependencies if t in d.required_in)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "dependencies": [d.to_dict() for d in self._dependencies],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DependencyManifest:
        ver = parse_contract_version(
            raw, _DEPENDENCY_MANIFEST_VERSION, description="dependency manifest version"
        )
        deps = raw.get("dependencies")
        if not isinstance(deps, list) or any(not isinstance(i, Mapping) for i in deps):
            raise ValueError("dependencies must be an array of objects")
        return cls((Dependency.from_dict(i) for i in deps), contract_version=ver)


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
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> ProbeResult:
        try:
            dur = float(raw.get("duration_ms", 0.0))
        except (TypeError, ValueError):
            raise ValueError("duration_ms must be numeric") from None
        return cls(
            parse_string(raw, "name"),
            DependencyStatus(parse_string(raw, "status")),
            parse_string(raw, "detail", required=False),
            dur,
        )


@runtime_checkable
class DependencyCheck(Protocol):
    name: str

    def probe(self) -> ProbeResult | Awaitable[ProbeResult]: ...


async def run_probes(
    checks: Iterable[DependencyCheck], *, timeout_seconds: float = 10.0
) -> tuple[ProbeResult, ...]:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    vals = tuple(checks)
    if len(vals) != len({c.name for c in vals}):
        raise ValueError("duplicate probe name")

    async def execute(c: DependencyCheck) -> ProbeResult:
        started = time.perf_counter()
        try:
            res = (
                await c.probe()
                if inspect.iscoroutinefunction(c.probe)
                else await _run_sync_probe(c.probe, name=c.name)
            )
            if inspect.isawaitable(res):
                res = await res
            if not isinstance(res, ProbeResult):
                raise TypeError("probe must return ProbeResult")
            if res.name != c.name:
                raise ValueError("probe result name does not match check name")
            return res
        except Exception as exc:  # noqa: BLE001
            return ProbeResult(
                c.name, DependencyStatus.ABSENT, f"{type(exc).__name__}: {exc}", _elapsed(started)
            )

    async def bounded(c: DependencyCheck) -> ProbeResult:
        try:
            return await asyncio.wait_for(execute(c), timeout=timeout_seconds)
        except TimeoutError:
            return ProbeResult(
                c.name,
                DependencyStatus.ABSENT,
                f"probe timed out after {timeout_seconds:g}s",
                timeout_seconds * 1000,
            )

    return tuple(await asyncio.gather(*(bounded(c) for c in vals)))


_MAX_CONCURRENT_SYNC_PROBES = 16
_SYNC_SEMAPHORES: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _get_sync_probe_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if (sem := _SYNC_SEMAPHORES.get(loop)) is None:
        sem = _SYNC_SEMAPHORES[loop] = asyncio.Semaphore(_MAX_CONCURRENT_SYNC_PROBES)
    return sem


async def _run_sync_probe(probe, *, name: str):
    loop, fut, ctx = (
        asyncio.get_running_loop(),
        asyncio.get_running_loop().create_future(),
        copy_context(),
    )

    def publish(*, result=None, error: BaseException | None = None) -> None:
        if not fut.done():
            fut.set_exception(error) if error is not None else fut.set_result(result)

    def worker() -> None:
        try:
            cb = partial(publish, result=ctx.run(probe))
        except (KeyboardInterrupt, SystemExit, Exception) as exc:  # noqa: BLE001
            cb = partial(publish, error=exc)
        except BaseException as exc:  # pragma: no cover
            cb = partial(
                publish, error=RuntimeError(f"sync probe aborted with {type(exc).__name__}")
            )
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(cb)

    async with _get_sync_probe_semaphore():
        Thread(target=worker, name=f"infra2-probe-{name}", daemon=True).start()
        try:
            return await fut
        finally:
            if not fut.done():
                fut.cancel()


def _elapsed(started: float) -> float:
    return (time.perf_counter() - started) * 1000


class _HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


def _health_response(
    results: Iterable[ProbeResult], *, required: Iterable[str]
) -> tuple[int, dict[str, Any]]:
    vals, req = tuple(results), frozenset(required)
    names = [r.name for r in vals]
    if len(names) != len(set(names)):
        raise ValueError("duplicate probe result name")
    if not vals and not req:
        raise ValueError("no probe results and no required dependencies: nothing to report on")
    checks = {
        r.name: {k: v for k, v in r.to_dict().items() if k != "name"} | {"required": r.name in req}
        for r in vals
    }
    for n in sorted(req - set(names)):
        checks[n] = {
            "status": DependencyStatus.ABSENT.value,
            "detail": "required dependency was not probed",
            "duration_ms": 0.0,
            "required": True,
        }
    absent = sorted(
        (n for n, e in checks.items() if e["status"] != DependencyStatus.PRESENT),
        key=lambda n: (not checks[n]["required"], n),
    )
    st = (
        _HealthStatus.UNHEALTHY
        if any(checks[n]["required"] for n in absent)
        else _HealthStatus.DEGRADED
        if absent
        else _HealthStatus.HEALTHY
    )
    body = {
        "status": st.value,
        "checks": dict(sorted(checks.items())),
        "reasons": [f"{n}: {checks[n]['detail'] or 'absent'}" for n in absent],
    }
    return (_HTTP_SERVICE_UNAVAILABLE if st is _HealthStatus.UNHEALTHY else _HTTP_OK), body


async def check_health(
    checks: Iterable[DependencyCheck],
    *,
    required: Iterable[str] | None = None,
    manifest: DependencyManifest | None = None,
    tier: str | EnvironmentTier | None = None,
    timeout_seconds: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    vals = tuple(checks)
    if manifest is not None:
        if required is not None:
            raise ValueError("pass either required or manifest, not both")
        if tier is None:
            raise ValueError("tier is required with a dependency manifest")
        req = manifest.required_for(tier)
    else:
        req = frozenset(required) if required is not None else frozenset(c.name for c in vals)
    return _health_response(await run_probes(vals, timeout_seconds=timeout_seconds), required=req)


async def probe_orchestrator(
    url: str,
    *,
    name: str = "orchestrator",
    client: Any | None = None,
    settings: Any | None = None,
) -> ProbeResult:
    from infra2_sdk.runtime.http import probe_http

    return await probe_http(url, name=name, client=client, settings=settings)


__all__ = [
    "Dependency",
    "DependencyKind",
    "DependencyManifest",
    "DependencyStatus",
    "ProbeResult",
    "probe_orchestrator",
]
