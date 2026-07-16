"""Strict primitives shared by versioned wire contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def require_contract_version(value: Any, expected: int, *, description: str) -> int:
    if type(value) is not int:
        raise ValueError("contract_version must be an integer")
    if value != expected:
        raise ValueError(f"unsupported {description} {value}")
    return value


def parse_contract_version(
    raw: Mapping[str, Any],
    expected: int,
    *,
    description: str,
) -> int:
    return require_contract_version(
        raw.get("contract_version", 0),
        expected,
        description=description,
    )
