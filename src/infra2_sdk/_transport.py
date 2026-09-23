"""Backward-compatibility shim for infra2_sdk.transport."""

from __future__ import annotations

from infra2_sdk.transport import (
    HttpResponse,
    HttpTransport,
    urllib_transport,
)

__all__ = [
    "HttpResponse",
    "HttpTransport",
    "urllib_transport",
]
