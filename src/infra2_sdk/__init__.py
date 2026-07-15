"""Stable contracts shared by infra2 and application repositories."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("infra2-sdk")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"

__all__ = ["__version__"]
