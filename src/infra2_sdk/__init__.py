"""Clean MECE runtime, telemetry, service, storage, LLM, and delivery contracts."""

from __future__ import annotations

import importlib
from importlib.metadata import PackageNotFoundError, version
from types import ModuleType
from typing import Any

try:
    __version__ = version("infra2-sdk")
except PackageNotFoundError:  # pragma: no cover
    __version__ = "0.0.0"

_SUBMODULES = frozenset({"config", "telemetry", "service", "storage", "llm", "delivery"})


def __getattr__(name: str) -> Any:
    if name in _SUBMODULES:
        mod: ModuleType = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*globals(), *_SUBMODULES])


__all__ = (
    "config",
    "telemetry",
    "service",
    "storage",
    "llm",
    "delivery",
)
