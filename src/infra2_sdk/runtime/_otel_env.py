"""Pure parsing helpers for the OpenTelemetry environment specification."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from urllib.parse import unquote_to_bytes
from warnings import warn

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


def load_resource_attributes(
    value: str,
    *,
    strict: bool,
) -> tuple[dict[str, str], str | None]:
    """Load optional OTel attributes, discarding the whole invalid value when non-strict."""

    try:
        attributes = parse_resource_attributes(value)
        deployment_environment = resource_attribute(
            attributes,
            "deployment.environment.name",
            "deployment.environment",
        )
    except ValueError as exc:
        if strict:
            raise
        warn(f"invalid OTEL_RESOURCE_ATTRIBUTES: {exc}", RuntimeWarning, stacklevel=3)
        return {}, None
    return attributes, deployment_environment


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
    """Validate the standard ``OTEL_TRACES_SAMPLER`` / ``OTEL_TRACES_SAMPLER_ARG`` pair.

    Returns the normalized sampler name and, for the ratio samplers, the sampling
    probability (1.0 when no argument is given, as in the specification). An unset name
    selects the specification default. The argument is ignored by samplers that take none.
    Unknown names and unparsable or out-of-range ratios raise ``ValueError``.
    """

    normalized = (name or "").strip().lower()
    if not normalized:
        return DEFAULT_TRACES_SAMPLER, None
    if normalized not in TRACES_SAMPLER_NAMES:
        raise ValueError(
            f"unsupported OTEL_TRACES_SAMPLER {normalized!r}; "
            f"expected one of {', '.join(TRACES_SAMPLER_NAMES)}"
        )
    if normalized not in _RATIO_SAMPLERS:
        return normalized, None
    raw = (arg or "").strip()
    if not raw:
        return normalized, 1.0
    try:
        ratio = float(raw)
    except ValueError:
        raise ValueError("OTEL_TRACES_SAMPLER_ARG must be a number between 0 and 1") from None
    if not math.isfinite(ratio) or not 0.0 <= ratio <= 1.0:
        raise ValueError("OTEL_TRACES_SAMPLER_ARG must be a number between 0 and 1")
    return normalized, ratio
