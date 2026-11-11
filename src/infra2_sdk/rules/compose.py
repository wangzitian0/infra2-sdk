"""Pure rules for compose file validation and resource ceiling enforcement."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from infra2_sdk._wire import _yaml

_SIZE_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([kmgtb]?b?)\s*$", re.I)
_UNITS = {
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


def _is_memory_ceiling(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, int | float):
        return value > 0
    if not isinstance(value, str):
        return False
    match = _SIZE_RE.match(value)
    return bool(match and float(match.group(1)) * _UNITS.get(match.group(2).lower(), 0) > 0)


def _tag_of_image_ref(ref: str) -> str:
    last = ref.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[-1] if ":" in last else ""


def find_bare_latest_violations(compose_text: str) -> list[str]:
    violations: list[str] = []
    for line in compose_text.splitlines():
        match = _IMAGE_RE.match(line)
        if not match:
            continue
        ref = match.group(1)
        if "${" not in ref and "@sha256:" not in ref and _tag_of_image_ref(ref) == "latest":
            violations.append(ref)
    return violations


@dataclass(frozen=True)
class _ComposeReport:
    path: str
    bare_latest_violations: tuple[str, ...]
    compliant_services: tuple[str, ...]
    unlimited_services: tuple[str, ...]
    errors: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return not (self.bare_latest_violations or self.unlimited_services or self.errors)


def _inspect_service_resource_limits(
    services: Mapping[str, Any] | str | Path,
) -> tuple[list[str], list[str]]:
    if isinstance(services, (str, Path)):
        report = inspect_compose(services)
        if report.errors:
            raise ValueError(f"Failed to inspect compose: {'; '.join(report.errors)}")
        return list(report.compliant_services), list(report.unlimited_services)

    if (
        isinstance(services, Mapping)
        and "services" in services
        and isinstance(services["services"], Mapping)
    ):
        services = services["services"]

    compliant: list[str] = []
    non_compliant: list[str] = []
    for name, spec in sorted(services.items()):
        if not isinstance(spec, dict):
            non_compliant.append(name)
        elif "extends" in spec:
            continue
        elif _is_memory_ceiling(spec.get("mem_limit")):
            compliant.append(name)
        else:
            non_compliant.append(name)
    return compliant, non_compliant


def inspect_compose(path_or_text: str | Path) -> _ComposeReport:
    path_str = str(path_or_text)
    is_path = isinstance(path_or_text, Path) or (
        isinstance(path_or_text, str)
        and "\n" not in path_or_text
        and (
            Path(path_or_text).exists()
            or "/" in path_or_text
            or "\\" in path_or_text
            or path_or_text.endswith((".yaml", ".yml"))
        )
    )
    if is_path:
        try:
            text = Path(path_or_text).read_text(encoding="utf-8")
        except OSError as exc:
            return _ComposeReport(path_str, (), (), (), (f"cannot read file: {exc}",))
    else:
        text = str(path_or_text)
        path_str = "<inline>"

    bare_violations = tuple(find_bare_latest_violations(text))
    yaml = _yaml()
    try:
        docs = [d for d in yaml.safe_load_all(text) if d]
    except yaml.YAMLError as exc:
        return _ComposeReport(path_str, bare_violations, (), (), (f"YAML parse error: {exc}",))

    compliant: list[str] = []
    unlimited: list[str] = []
    for doc in docs:
        if not isinstance(doc, dict) or ("include" in doc and not doc.get("services")):
            continue
        services = doc.get("services")
        if isinstance(services, dict):
            comp, unlim = _inspect_service_resource_limits(services)
            compliant.extend(comp)
            unlimited.extend(unlim)

    return _ComposeReport(
        path=path_str,
        bare_latest_violations=bare_violations,
        compliant_services=tuple(sorted(compliant)),
        unlimited_services=tuple(sorted(unlimited)),
    )


__all__ = [
    "inspect_compose",
]
