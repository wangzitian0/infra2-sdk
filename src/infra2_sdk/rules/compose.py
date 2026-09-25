"""Pure rules for compose file validation and resource ceiling enforcement."""

from __future__ import annotations

import argparse
import glob
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

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


@dataclass(frozen=True)
class ComposeReport:
    """Inspection report for a single compose file or fragment."""

    path: str
    bare_latest_violations: tuple[str, ...]
    compliant_services: tuple[str, ...]
    unlimited_services: tuple[str, ...]
    errors: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return (
            len(self.bare_latest_violations) == 0
            and len(self.unlimited_services) == 0
            and len(self.errors) == 0
        )


def inspect_service_resource_limits(
    services: Mapping[str, Any] | str | Path,
) -> tuple[list[str], list[str]]:
    """Return (compliant_services, non_compliant_services) from services mapping or compose source.

    A service is compliant if it sets a positive mem_limit. If a string or Path is
    provided, it is safely parsed as YAML.
    """
    if isinstance(services, (str, Path)):
        report = inspect_compose(services)
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
            continue
        if "extends" in spec:
            continue  # ceiling may reside in parent
        if is_memory_ceiling(spec.get("mem_limit")):
            compliant.append(name)
        else:
            non_compliant.append(name)
    return compliant, non_compliant


def inspect_compose(path_or_text: str | Path) -> ComposeReport:
    """Parse and inspect a compose file or text for both bare latest tags and resource limits."""
    path_str = str(path_or_text)
    is_existing_file = isinstance(path_or_text, Path) or (
        isinstance(path_or_text, str) and "\n" not in path_or_text and Path(path_or_text).exists()
    )
    if is_existing_file:
        try:
            text = Path(path_or_text).read_text(encoding="utf-8")
        except OSError as exc:
            return ComposeReport(
                path=path_str,
                bare_latest_violations=(),
                compliant_services=(),
                unlimited_services=(),
                errors=(f"cannot read file: {exc}",),
            )
    else:
        text = str(path_or_text)
        path_str = "<inline>"

    bare_violations = tuple(find_bare_latest_violations(text))

    try:
        docs = [d for d in yaml.safe_load_all(text) if d]
    except yaml.YAMLError as exc:
        return ComposeReport(
            path=path_str,
            bare_latest_violations=bare_violations,
            compliant_services=(),
            unlimited_services=(),
            errors=(f"YAML parse error: {exc}",),
        )

    compliant: list[str] = []
    unlimited: list[str] = []
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        # include-only fragment without services
        if "include" in doc and not doc.get("services"):
            continue
        services = doc.get("services")
        if not isinstance(services, dict):
            continue
        for name, spec in services.items():
            if not isinstance(spec, dict):
                unlimited.append(str(name))
                continue
            if "extends" in spec:
                continue
            if is_memory_ceiling(spec.get("mem_limit")):
                compliant.append(str(name))
            else:
                unlimited.append(str(name))

    return ComposeReport(
        path=path_str,
        bare_latest_violations=bare_violations,
        compliant_services=tuple(sorted(compliant)),
        unlimited_services=tuple(sorted(unlimited)),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """CLI runner for compose linting."""
    parser = argparse.ArgumentParser(
        description="Lint Compose files for resource limits and image pin rules."
    )
    parser.add_argument(
        "files", nargs="*", help="Compose files to inspect (default: scans **/compose.yaml)"
    )
    parser.add_argument(
        "--glob", dest="glob_pattern", default=None, help="Optional glob pattern to search files"
    )
    parser.add_argument(
        "--allow-unlimited",
        nargs="*",
        default=(),
        help="List of service names or path::service to grandfather",
    )
    args = parser.parse_args(argv)

    paths: list[Path] = []
    if args.files:
        for f in args.files:
            paths.append(Path(f))
    elif args.glob_pattern:
        for p in sorted(glob.glob(args.glob_pattern, recursive=True)):
            paths.append(Path(p))
    else:
        for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml"):
            for p in sorted(Path(".").rglob(name)):
                ignored = any(
                    part.startswith(".") or part in ("node_modules", ".venv", "venv")
                    for part in p.parts
                )
                if not ignored:
                    paths.append(p)

    if not paths:
        print("No compose files found to inspect.")
        return 0

    allowed_set = set(args.allow_unlimited)
    total_violations = 0
    total_bare = 0
    for path in paths:
        report = inspect_compose(path)
        if report.errors:
            print(f"❌ {path}: {', '.join(report.errors)}")
            total_violations += len(report.errors)
        if report.bare_latest_violations:
            for ref in report.bare_latest_violations:
                print(f"❌ {path}: Bare ':latest' tag: {ref}")
                total_bare += 1
        unlimited = [
            svc
            for svc in report.unlimited_services
            if svc not in allowed_set and f"{path}::{svc}" not in allowed_set
        ]
        if unlimited:
            for svc in unlimited:
                print(f"❌ {path}: Service '{svc}' missing memory ceiling (mem_limit)")
                total_violations += 1

    if total_violations > 0 or total_bare > 0:
        msg = f"\n❌ FAILED: {total_bare} bare ':latest' and {total_violations} ceiling violations."
        print(msg)
        return 1

    print(f"✅ PASSED: Inspected {len(paths)} compose file(s) with no violations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
