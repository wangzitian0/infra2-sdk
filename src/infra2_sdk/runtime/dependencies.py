"""Backwards-compatible shim for runtime dependencies.

All capabilities have been consolidated into :mod:`infra2_sdk.runtime.health`.
"""

from __future__ import annotations

from infra2_sdk.runtime.health import (
    DEPENDENCY_MANIFEST_VERSION,
    Dependency,
    DependencyKind,
    DependencyManifest,
)

__all__ = [
    "DEPENDENCY_MANIFEST_VERSION",
    "Dependency",
    "DependencyKind",
    "DependencyManifest",
]
