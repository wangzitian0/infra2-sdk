"""Clean MECE runtime, telemetry, service, storage, LLM, and delivery contracts."""

from __future__ import annotations

import importlib as _importlib
from types import ModuleType as _ModuleType
from typing import Any as _Any

_SUBMODULES = frozenset({"config", "telemetry", "service", "storage", "llm", "delivery"})


def __getattr__(name: str) -> _Any:
    if name == "__version__":
        from importlib.metadata import PackageNotFoundError, version

        try:
            resolved = version("infra2-sdk")
        except PackageNotFoundError:  # pragma: no cover
            resolved = "0.0.0"
        globals()["__version__"] = resolved
        return resolved
    if name in _SUBMODULES:
        mod: _ModuleType = _importlib.import_module(f"{__name__}.{name}")
        globals()[name] = mod
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted([*__all__, "__version__"])


__all__ = (
    "config",
    "telemetry",
    "service",
    "storage",
    "llm",
    "delivery",
)
