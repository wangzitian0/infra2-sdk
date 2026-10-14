"""Strict primitives shared by versioned wire contracts."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


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


def parse_string(
    raw: Mapping[str, Any],
    key: str,
    *,
    required: bool = True,
    default: str = "",
) -> str:
    value = raw.get(key)
    if value is None:
        if required:
            raise ValueError(f"{key} is required")
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key} is required")
    return value


def require_exact_fields(
    raw: Mapping[str, Any],
    expected: set[str],
    *,
    description: str,
    contract_message: str | None = None,
) -> None:
    if not isinstance(raw, Mapping):
        raise ValueError(f"{description} must be an object")
    if set(raw) != expected:
        message = contract_message or f"{description} fields must exactly match v1"
        raise ValueError(message)


__all__ = [
    "JSON_SCHEMA_DIALECT",
    "parse_contract_version",
    "parse_string",
    "require_contract_version",
    "require_exact_fields",
]
