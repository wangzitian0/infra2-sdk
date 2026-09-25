"""Stable contracts shared by infra2 and application repositories."""

from importlib.metadata import PackageNotFoundError, version

from infra2_sdk.transport import (
    HttpResponse,
    HttpTransport,
    urllib_transport,
)

try:
    __version__ = version("infra2-sdk")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"

__all__ = [
    "HttpResponse",
    "HttpTransport",
    "__version__",
    "images",
    "urllib_transport",
]

