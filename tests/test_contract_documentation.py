"""Tests ensuring README.md contract documentation strictly matches code SSOT."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from infra2_sdk.runtime.environment import runtime_env_contract


def _extract_readme_env_table() -> tuple[set[str], set[str]]:
    """Extract canonical variable names and aliases from README.md Environment contract table."""
    readme_path = Path(__file__).resolve().parent.parent / "README.md"
    content = readme_path.read_text(encoding="utf-8")

    # Locate the "## Environment contract" section and its markdown table
    section_match = re.search(
        r"## Environment contract.*?\n\| Concern \| Canonical names \| Compatibility aliases \|\n\|---\|---\|---\|\n(.*?)\n\n",
        content,
        re.DOTALL,
    )
    assert section_match is not None, "Could not find Environment contract table in README.md"

    table_body = section_match.group(1).strip()
    canonical_names: set[str] = set()
    aliases: set[str] = set()

    for line in table_body.splitlines():
        line = line.strip()
        if not line.startswith("|") or not line.endswith("|"):
            continue
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) != 3:
            continue
        _concern, raw_canonicals, raw_aliases = cols

        # Extract backticked names: `NAME` or `NAME=value`
        for name in re.findall(r"`([A-Z0-9_]+)(?:=[^`]+)?`", raw_canonicals):
            canonical_names.add(name)

        if raw_aliases and raw_aliases != "—":
            for alias in re.findall(r"`([A-Z0-9_]+)`", raw_aliases):
                aliases.add(alias)

    return canonical_names, aliases


def test_readme_documents_every_canonical_environment_variable() -> None:
    """Verify that README.md environment table includes every canonical variable in runtime_env_contract."""
    documented_canonicals, documented_aliases = _extract_readme_env_table()
    contract = runtime_env_contract()

    expected_canonicals = {item["name"] for item in contract["variables"]}
    expected_aliases = {alias for item in contract["variables"] for alias in item["aliases"]}

    missing_canonicals = expected_canonicals - documented_canonicals
    extra_canonicals = documented_canonicals - expected_canonicals

    assert not missing_canonicals, f"README.md is missing canonical variables: {sorted(missing_canonicals)}"
    assert not extra_canonicals, f"README.md documents unknown canonical variables: {sorted(extra_canonicals)}"

    missing_aliases = expected_aliases - documented_aliases
    extra_aliases = documented_aliases - expected_aliases

    assert not missing_aliases, f"README.md is missing compatibility aliases: {sorted(missing_aliases)}"
    assert not extra_aliases, f"README.md documents unknown compatibility aliases: {sorted(extra_aliases)}"


def test_readme_documents_valid_core_runtime_entrypoints() -> None:
    """Verify that every entrypoint documented in README.md Core runtime entrypoints table exists."""
    import importlib

    readme_path = Path(__file__).resolve().parent.parent / "README.md"
    content = readme_path.read_text(encoding="utf-8")

    section_match = re.search(
        r"### Core runtime entrypoints.*?\n\| Capability \| Canonical entrypoint \| Primary module \| Description \|\n\|---\|---\|---\|---\|\n(.*?)\n\n",
        content,
        re.DOTALL,
    )
    assert section_match is not None, "Could not find Core runtime entrypoints table in README.md"

    table_body = section_match.group(1).strip()
    for line in table_body.splitlines():
        cols = [c.strip() for c in line.split("|")[1:-1]]
        if len(cols) != 4:
            continue
        _cap, raw_entrypoints, module_name, _desc = cols
        module_name = module_name.strip("` ")
        mod = importlib.import_module(module_name)
        for symbol in re.findall(r"`([A-Za-z0-9_]+)`", raw_entrypoints):
            assert hasattr(mod, symbol), f"Module {module_name!r} does not have symbol {symbol!r}"

