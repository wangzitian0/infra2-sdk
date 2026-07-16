"""Pure parsing helpers for the OpenTelemetry environment specification."""

from __future__ import annotations

import re
from collections.abc import Mapping
from urllib.parse import unquote_to_bytes

_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9a-fA-F]{2})")


def parse_resource_attributes(value: str) -> dict[str, str]:
    """Parse the standard percent-encoded OTEL_RESOURCE_ATTRIBUTES format."""

    if not value.strip():
        return {}
    attributes: dict[str, str] = {}
    for item in value.split(","):
        if "=" not in item:
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES entries must use key=value")
        encoded_key, raw = item.split("=", 1)
        encoded_key = encoded_key.strip()
        raw = raw.strip()
        if _INVALID_PERCENT_ESCAPE_RE.search(encoded_key) or _INVALID_PERCENT_ESCAPE_RE.search(raw):
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES contains an invalid percent escape")
        try:
            key = unquote_to_bytes(encoded_key).decode("utf-8")
            decoded = unquote_to_bytes(raw).decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES contains invalid UTF-8") from None
        if not key or not decoded:
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES entries must be non-empty")
        if key in attributes:
            raise ValueError(f"OTEL_RESOURCE_ATTRIBUTES repeats {key!r}")
        attributes[key] = decoded
    return attributes


def resource_attribute(
    attributes: Mapping[str, str],
    canonical: str,
    *compatibility_names: str,
) -> str | None:
    """Resolve equivalent resource attributes and reject ambiguous values."""

    names = (canonical, *compatibility_names)
    present = [
        (name, attributes[name].strip()) for name in names if attributes.get(name, "").strip()
    ]
    if len({value for _, value in present}) > 1:
        joined = ", ".join(name for name, _ in present)
        raise ValueError(f"conflicting OTEL_RESOURCE_ATTRIBUTES values: {joined}")
    return present[0][1] if present else None


def parse_otel_boolean(value: str | None, *, default: bool = False) -> bool:
    """Apply the OTel boolean grammar rather than the SDK's permissive bool grammar."""

    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError("OpenTelemetry boolean environment variables must be true or false")
