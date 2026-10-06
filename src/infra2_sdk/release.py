"""Backwards-compatible shim for release identity and verification.

All capabilities have been consolidated into :mod:`infra2_sdk.refs`.
"""

from __future__ import annotations

from infra2_sdk.refs import (
    ReleaseError,
    ReleaseIdentity,
    resolve_image_digest,
    resolve_release_identity,
    verify_runtime_identity,
)

__all__ = [
    "ReleaseError",
    "ReleaseIdentity",
    "resolve_image_digest",
    "resolve_release_identity",
    "verify_runtime_identity",
]
