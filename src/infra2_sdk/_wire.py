"""Strict primitives shared by versioned wire contracts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def canonical_json_bytes(payload: Any) -> bytes:
    """Serialize payload into canonical UTF-8 JSON bytes with sorted keys and ASCII escaping."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def canonical_sha256(payload: Any) -> str:
    """Return lowercase hex SHA-256 of canonical UTF-8 JSON bytes."""
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


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


def validate_http_url(
    value: str,
    *,
    label: str,
    allow_fragment: bool = True,
    validate_port: bool = False,
) -> None:
    try:
        endpoint = urlsplit(value)
        hostname = endpoint.hostname
        if validate_port:
            _ = endpoint.port
    except ValueError:
        raise ValueError(f"{label} must be a valid HTTP URL") from None
    if (
        endpoint.scheme not in {"http", "https"}
        or not endpoint.netloc
        or not hostname
        or any(char.isspace() for char in endpoint.netloc)
    ):
        raise ValueError(f"{label} must use http:// or https:// with a host")
    if endpoint.username is not None or endpoint.password is not None:
        raise ValueError(f"{label} must not contain credentials")
    if not allow_fragment and endpoint.fragment:
        raise ValueError(f"{label} must not contain a fragment")


def _is_loopback_host(host: str) -> bool:
    """Return True when ``host`` is a loopback address (localhost, 127.0.0.1, ::1)."""
    return host.strip("[]").lower() in _LOOPBACK_HOSTS


def parse_iso_utc(value: object, *, allow_naive: bool = False) -> datetime:
    """Parse an ISO 8601 timestamp string into a timezone-aware UTC ``datetime``."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("expected non-empty ISO 8601 string")
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        if not allow_naive:
            raise ValueError("timestamp must have a time zone")
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _yaml() -> Any:
    """Import PyYAML on demand when a YAML helper runs."""
    import yaml

    return yaml


__all__ = [
    "_is_loopback_host",
    "_yaml",
    "canonical_json_bytes",
    "canonical_sha256",
    "parse_contract_version",
    "parse_iso_utc",
    "parse_string",
    "require_contract_version",
    "require_exact_fields",
    "validate_http_url",
]
