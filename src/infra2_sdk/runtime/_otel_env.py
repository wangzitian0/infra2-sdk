"""Pure parsing helpers for the OpenTelemetry environment specification."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from urllib.parse import unquote_to_bytes
from warnings import warn

_INVALID_PERCENT_ESCAPE_RE = re.compile(r"%(?![0-9a-fA-F]{2})")


def parse_resource_attributes(value: str) -> dict[str, str]:
    """Parse the percent-encoded OTEL_RESOURCE_ATTRIBUTES format."""
    if not value.strip():
        return {}
    attributes: dict[str, str] = {}
    for item in value.split(","):
        if "=" not in item:
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES entries must use key=value")
        ek, raw = (part.strip() for part in item.split("=", 1))
        if _INVALID_PERCENT_ESCAPE_RE.search(ek) or _INVALID_PERCENT_ESCAPE_RE.search(raw):
            raise ValueError("OTEL_RESOURCE_ATTRIBUTES contains an invalid percent escape")
        try:
            key = unquote_to_bytes(ek).decode("utf-8")
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
    attributes: Mapping[str, str], canonical: str, *compatibility_names: str
) -> str | None:
    """Resolve equivalent resource attributes and reject conflicting values."""
    present = [
        (name, attributes[name].strip())
        for name in (canonical, *compatibility_names)
        if attributes.get(name, "").strip()
    ]
    if len({val for _, val in present}) > 1:
        raise ValueError(
            f"conflicting OTEL_RESOURCE_ATTRIBUTES values: {', '.join(n for n, _ in present)}"
        )
    return present[0][1] if present else None


def load_resource_attributes(
    value: str, *, strict: bool, stacklevel: int
) -> tuple[dict[str, str], str | None]:
    """Load OTel resource attributes, warning and discarding invalid values when non-strict."""
    try:
        attrs = parse_resource_attributes(value)
        dep_env = resource_attribute(attrs, "deployment.environment.name", "deployment.environment")
    except ValueError as exc:
        if strict:
            raise
        warn(f"invalid OTEL_RESOURCE_ATTRIBUTES: {exc}", RuntimeWarning, stacklevel=stacklevel)
        return {}, None
    return attrs, dep_env


def parse_otel_boolean(value: str | None, *, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default
    norm = value.strip().lower()
    if norm == "true":
        return True
    if norm == "false":
        return False
    raise ValueError("OpenTelemetry boolean environment variables must be true or false")


DEFAULT_TRACES_SAMPLER = "parentbased_always_on"
TRACES_SAMPLER_NAMES = (
    "always_on",
    "always_off",
    "traceidratio",
    "parentbased_always_on",
    "parentbased_always_off",
    "parentbased_traceidratio",
)
_RATIO_SAMPLERS = frozenset({"traceidratio", "parentbased_traceidratio"})


def parse_traces_sampler(name: str | None, arg: str | None = None) -> tuple[str, float | None]:
    """Validate ``OTEL_TRACES_SAMPLER`` and ``OTEL_TRACES_SAMPLER_ARG``."""
    norm = (name or "").strip().lower()
    if not norm:
        return DEFAULT_TRACES_SAMPLER, None
    if norm not in TRACES_SAMPLER_NAMES:
        expected = ", ".join(TRACES_SAMPLER_NAMES)
        raise ValueError(f"unsupported OTEL_TRACES_SAMPLER {norm!r}; expected one of {expected}")
    if norm not in _RATIO_SAMPLERS:
        return norm, None
    raw = (arg or "").strip()
    if not raw:
        return norm, 1.0
    try:
        ratio = float(raw)
    except ValueError:
        raise ValueError("OTEL_TRACES_SAMPLER_ARG must be a number between 0 and 1") from None
    if not math.isfinite(ratio) or not 0.0 <= ratio <= 1.0:
        raise ValueError("OTEL_TRACES_SAMPLER_ARG must be a number between 0 and 1")
    return norm, ratio
