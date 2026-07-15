"""Helpers for optional runtime protocol dependencies."""

from __future__ import annotations

import importlib
from types import ModuleType


def require(module: str, *, extra: str) -> ModuleType:
    try:
        return importlib.import_module(module)
    except ImportError:
        raise RuntimeError(f"{module} is required; install infra2-sdk[{extra}]") from None
