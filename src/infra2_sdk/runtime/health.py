"""Framework-agnostic readiness: run the dependency probes, return an HTTP status and body.

The body follows infra2's health-check contract (``ops.observability`` section 5.1):

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

from collections.abc import Iterable
from enum import StrEnum
from typing import Any

from infra2_sdk.runtime.dependencies import (
    Dependency,
    DependencyKind,
    DependencyManifest,
)
from infra2_sdk.runtime.environment import EnvironmentTier
from infra2_sdk.runtime.probes import (
    DependencyCheck,
    DependencyStatus,
    DependencyUnavailableError,
    ProbeResult,
    assert_required_dependencies,
    run_probes,
)

HTTP_OK = 200
HTTP_SERVICE_UNAVAILABLE = 503


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
    "HTTP_OK",
    "HTTP_SERVICE_UNAVAILABLE",
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
