"""Framework-agnostic readiness: dependency manifests, probes, and health check runner.

The health check response follows infra2's contract (``ops.observability`` section 5.1):

.. code-block:: json

    {"status": "healthy" | "degraded" | "unhealthy",
     "checks": {"<name>": {"status": "present" | "absent", "required": true, "detail": "...",
                           "duration_ms": 1.2}},
     "reasons": ["<name>: <why it is absent>"]}

A required dependency that is absent, failed, timed out, or was never probed makes the whole
report ``unhealthy`` with status 503. A failing optional dependency makes it ``degraded``
(still 200: the service can take traffic). Nothing is reported healthy by default: a probe
that raises is an absent dependency, and a report with nothing to judge is an error rather
than a green. Mapping the pair onto a framework response is the caller's one line.
"""

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

from infra2_sdk._wire import _string, parse_contract_version, require_contract_version
from infra2_sdk.runtime.environment import EnvironmentTier, resolve_environment_tier

DEPENDENCY_MANIFEST_VERSION = 1
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"

HTTP_OK = 200
HTTP_SERVICE_UNAVAILABLE = 503


class DependencyKind(StrEnum):
    CODE_DOMINANT = "code_dominant"
    MODEL_DOMINANT = "model_dominant"


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
            self,
            "required_in",
            frozenset(resolve_environment_tier(tier) for tier in self.required_in),
        )
        object.__setattr__(self, "env_vars", frozenset(self.env_vars))
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("dependency name must be a non-empty identifier")
        if not self.required_in:
            raise ValueError(f"dependency {self.name!r} must be required in at least one tier")
        if not self.env_vars:
            raise ValueError(f"dependency {self.name!r} must declare environment variables")
        if any(not key or key.upper() != key for key in self.env_vars):
            raise ValueError("dependency environment variables must be uppercase")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "required_in": sorted(tier.value for tier in self.required_in),
            "env_vars": sorted(self.env_vars),
            "summary": self.summary,
            "local_backend": self.local_backend,
            "deployed_backend": self.deployed_backend,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Dependency:
        required = raw.get("required_in")
        env_vars = raw.get("env_vars")
        if not isinstance(required, list) or not isinstance(env_vars, list):
            raise ValueError("required_in and env_vars must be arrays")
        if any(not isinstance(value, str) for value in (*required, *env_vars)):
            raise ValueError("required_in and env_vars must contain strings")
        return cls(
            name=_string(raw, "name"),
            kind=DependencyKind(_string(raw, "kind")),
            required_in=frozenset(resolve_environment_tier(value) for value in required),
            env_vars=frozenset(env_vars),
            summary=_string(raw, "summary", required=False),
            local_backend=_string(raw, "local_backend", required=False),
            deployed_backend=_string(raw, "deployed_backend", required=False),
        )


class DependencyManifest:
    def __init__(
        self,
        dependencies: Iterable[Dependency],
        *,
        contract_version: int = DEPENDENCY_MANIFEST_VERSION,
    ) -> None:
        require_contract_version(
            contract_version,
            DEPENDENCY_MANIFEST_VERSION,
            description="dependency manifest version",
        )
        self.contract_version = contract_version
        values = tuple(dependencies)
        names = [dependency.name for dependency in values]
        if len(names) != len(set(names)):
            raise ValueError("duplicate dependency name")
        self._dependencies = values
        self._by_name = {dependency.name: dependency for dependency in values}

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
        resolved = resolve_environment_tier(tier)
        return frozenset(
            dependency.name
            for dependency in self._dependencies
            if resolved in dependency.required_in
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "dependencies": [dependency.to_dict() for dependency in self._dependencies],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DependencyManifest:
        version = parse_contract_version(
            raw,
            DEPENDENCY_MANIFEST_VERSION,
            description="dependency manifest version",
        )
        dependencies = raw.get("dependencies")
        if not isinstance(dependencies, list):
            raise ValueError("dependencies must be an array")
        if any(not isinstance(item, Mapping) for item in dependencies):
            raise ValueError("dependencies must be an array of objects")
        return cls(
            (Dependency.from_dict(item) for item in dependencies),
            contract_version=version,
        )

    @staticmethod
    def json_schema() -> dict[str, Any]:
        return {
            "$schema": JSON_SCHEMA_DIALECT,
            "title": "Runtime dependency manifest",
            "type": "object",
            "additionalProperties": False,
            "required": ["contract_version", "dependencies"],
            "properties": {
                "contract_version": {"const": DEPENDENCY_MANIFEST_VERSION},
                "dependencies": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "kind", "required_in", "env_vars"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "kind": {"enum": [kind.value for kind in DependencyKind]},
                            "required_in": {
                                "type": "array",
                                "items": {"enum": [tier.value for tier in EnvironmentTier]},
                                "minItems": 1,
                                "uniqueItems": True,
                            },
                            "env_vars": {
                                "type": "array",
                                "items": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"},
                                "minItems": 1,
                                "uniqueItems": True,
                            },
                            "summary": {"type": "string"},
                            "local_backend": {"type": "string"},
                            "deployed_backend": {"type": "string"},
                        },
                    },
                },
            },
        }


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


_MAX_CONCURRENT_SYNC_PROBES = 16
_SYNC_SEMAPHORES: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _get_sync_probe_semaphore() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    sem = _SYNC_SEMAPHORES.get(loop)
    if sem is None:
        sem = asyncio.Semaphore(_MAX_CONCURRENT_SYNC_PROBES)
        _SYNC_SEMAPHORES[loop] = sem
    return sem


async def _run_sync_probe(probe, *, name: str):
    """Run a sync probe without making CLI shutdown wait for timed-out worker threads."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    context = copy_context()

    def publish(*, result=None, error: BaseException | None = None) -> None:
        if future.done():
            return
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)

    def worker() -> None:
        try:
            result = context.run(probe)
        except (KeyboardInterrupt, SystemExit) as exc:
            callback = partial(publish, error=exc)
        except Exception as exc:  # noqa: BLE001 - forwarded to the async runner
            callback = partial(publish, error=exc)
        except BaseException as exc:  # pragma: no cover - defensive thread boundary
            error = RuntimeError(f"sync probe aborted with {type(exc).__name__}")
            callback = partial(publish, error=error)
        else:
            callback = partial(publish, result=result)
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(callback)

    sem = _get_sync_probe_semaphore()
    async with sem:
        Thread(target=worker, name=f"infra2-probe-{name}", daemon=True).start()
        try:
            return await future
        finally:
            if not future.done():
                future.cancel()


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


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


def health_response(
    results: Iterable[ProbeResult],
    *,
    required: Iterable[str],
) -> tuple[int, dict[str, Any]]:
    """Summarize finished probes as ``(status_code, body)``.

    ``required`` names the dependencies that must be present; one with no result counts as
    absent, exactly as in ``assert_required_dependencies``. Raises ``ValueError`` for
    duplicate result names and when there are neither results nor required names.
    """
    values = tuple(results)
    required_names = frozenset(required)
    names = [result.name for result in values]
    if len(names) != len(set(names)):
        raise ValueError("duplicate probe result name")
    if not values and not required_names:
        raise ValueError("no probe results and no required dependencies: nothing to report on")

    checks: dict[str, dict[str, Any]] = {}
    for result in values:
        entry = result.to_dict()
        entry.pop("name")
        entry["required"] = result.name in required_names
        checks[result.name] = entry
    for name in sorted(required_names - set(names)):
        checks[name] = {
            "status": DependencyStatus.ABSENT.value,
            "detail": "required dependency was not probed",
            "duration_ms": 0.0,
            "required": True,
        }

    absent = sorted(
        (name for name, entry in checks.items() if entry["status"] != DependencyStatus.PRESENT),
        key=lambda name: (not checks[name]["required"], name),
    )
    if any(checks[name]["required"] for name in absent):
        status = HealthStatus.UNHEALTHY
    elif absent:
        status = HealthStatus.DEGRADED
    else:
        status = HealthStatus.HEALTHY
    body = {
        "status": status.value,
        "checks": dict(sorted(checks.items())),
        "reasons": [f"{name}: {checks[name]['detail'] or 'absent'}" for name in absent],
    }
    return (HTTP_SERVICE_UNAVAILABLE if status is HealthStatus.UNHEALTHY else HTTP_OK), body


async def check_health(
    checks: Iterable[DependencyCheck],
    *,
    required: Iterable[str] | None = None,
    manifest: DependencyManifest | None = None,
    tier: str | EnvironmentTier | None = None,
    timeout_seconds: float = 10.0,
) -> tuple[int, dict[str, Any]]:
    """Run ``checks`` with ``run_probes`` and return ``(status_code, body)``.

    Which dependencies are required is either ``required`` (names), or ``manifest`` together
    with ``tier`` (``manifest.required_for(tier)``), or, when neither is given, every check:
    the fail-closed default. Probe failures never raise out of here, they are reported as
    absent dependencies; misuse (duplicate names, an unknown tier, ambiguous requirements,
    nothing to check) raises ``ValueError`` so a broken handler fails instead of reporting 200.
    """
    values = tuple(checks)
    if manifest is not None:
        if required is not None:
            raise ValueError("pass either required or manifest, not both")
        if tier is None:
            raise ValueError("tier is required with a dependency manifest")
        required_names = manifest.required_for(tier)
    elif required is not None:
        required_names = frozenset(required)
    else:
        required_names = frozenset(check.name for check in values)
    results = await run_probes(values, timeout_seconds=timeout_seconds)
    return health_response(results, required=required_names)


__all__ = [
    "DEPENDENCY_MANIFEST_VERSION",
    "HTTP_OK",
    "HTTP_SERVICE_UNAVAILABLE",
    "JSON_SCHEMA_DIALECT",
    "Dependency",
    "DependencyCheck",
    "DependencyKind",
    "DependencyManifest",
    "DependencyStatus",
    "DependencyUnavailableError",
    "HealthStatus",
    "ProbeResult",
    "assert_required_dependencies",
    "check_health",
    "health_response",
    "run_probes",
]
