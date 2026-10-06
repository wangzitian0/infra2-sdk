"""Backwards-compatible shim for runtime dependency probes.

All capabilities have been consolidated into :mod:`infra2_sdk.runtime.health`.
"""

from __future__ import annotations

from infra2_sdk.runtime.health import (
    JSON_SCHEMA_DIALECT,
    DependencyCheck,
    DependencyStatus,
    DependencyUnavailableError,
    ProbeResult,
    assert_required_dependencies,
    run_probes,
)

__all__ = [
    "JSON_SCHEMA_DIALECT",
    "DependencyCheck",
    "DependencyStatus",
    "DependencyUnavailableError",
    "ProbeResult",
    "assert_required_dependencies",
    "run_probes",
]
