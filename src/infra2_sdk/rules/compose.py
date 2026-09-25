"""Pure rules for compose file validation and resource ceiling enforcement."""

from __future__ import annotations

import re
from typing import Any

# 512m, 1.5g, 2G, 1073741824. A bare 0, "0", "0b" or prose is not a ceiling.
SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtb]?b?)\s*$", re.I)
UNITS = {
    "": 1,
    "b": 1,
    "k": 2**10,
    "kb": 2**10,
    "m": 2**20,
    "mb": 2**20,
    "g": 2**30,
    "gb": 2**30,
    "t": 2**40,
    "tb": 2**40,
}

_IMAGE_RE = re.compile(r"^\s*image:\s*(\S+)\s*$")


def is_memory_ceiling(value: object) -> bool:
    """Return whether value actually caps memory. Docker treats 0 as unlimited."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int | float):
        return value > 0
    if not isinstance(value, str):
        return False
    match = SIZE_RE.match(value)
    if not match:
        return False
    return float(match.group(1)) * UNITS.get(match.group(2).lower(), 0) > 0


def tag_of_image_ref(ref: str) -> str:
    """Return the tag of an image ref, or '' if untagged. Ignores a registry:port colon."""
    last = ref.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[-1] if ":" in last else ""


def find_bare_latest_violations(compose_text: str) -> list[str]:
    """Find image refs in compose_text that use a bare :latest tag with no digest.

    Allowed (skipped): digest-pinned refs (``...@sha256:...``) and variable-templated
    refs (``${IMAGE_TAG}`` etc.).
    """
    violations: list[str] = []
    for line in compose_text.splitlines():
        match = _IMAGE_RE.match(line)
        if not match:
            continue
        ref = match.group(1)
        if "${" in ref or "@sha256:" in ref:
            continue
        if tag_of_image_ref(ref) == "latest":
            violations.append(ref)
    return violations


def inspect_service_resource_limits(
    services: dict[str, Any],
) -> tuple[list[str], list[str]]:
    """Return (compliant_services, non_compliant_services) from services mapping.

    A service is compliant if it sets a positive mem_limit.
    """
    compliant: list[str] = []
    non_compliant: list[str] = []
    for name, spec in sorted(services.items()):
        if not isinstance(spec, dict):
            non_compliant.append(name)
            continue
        if is_memory_ceiling(spec.get("mem_limit")):
            compliant.append(name)
        else:
            non_compliant.append(name)
    return compliant, non_compliant
