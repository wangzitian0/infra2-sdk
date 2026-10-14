"""Tests for canonical package exports, __all__ declarations, and isolation."""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

import infra2_sdk
import infra2_sdk.runtime


def test_infra2_sdk_root_all_matches_actual_attributes() -> None:
    """Verify that every symbol declared in __all__ exists on infra2_sdk."""
    assert len(infra2_sdk.__all__) == len(set(infra2_sdk.__all__)), "duplicate name in __all__"
    for name in infra2_sdk.__all__:
        assert hasattr(infra2_sdk, name), f"infra2_sdk.__all__ has {name!r} but missing"


def test_infra2_sdk_submodules_accessible_from_root() -> None:
    """Verify that all canonical non-private submodules are accessible on infra2_sdk."""
    expected_submodules = [
        "capacity",
        "ci",
        "delivery",
        "deploy",
        "fastapi",
        "manifests",
        "refs",
        "routing",
        "rules",
        "runtime",
        "scaffold",
        "secrets",
        "transport",
    ]
    for sub in expected_submodules:
        assert hasattr(infra2_sdk, sub), f"infra2_sdk should expose submodule {sub!r}"
        mod = getattr(infra2_sdk, sub)
        assert isinstance(mod, ModuleType), f"infra2_sdk.{sub} should be a module"


def _namespace_differences(module: ModuleType) -> tuple[set[str], set[str]]:
    """(public non-module attributes not in __all__, non-module names of __all__ not public).

    A public attribute is a name that does not start with an underscore. Submodules are
    not counted: __all__ may list them, and importing a submodule binds it in the package.
    """
    public = {
        name
        for name, value in vars(module).items()
        if not name.startswith("_") and not isinstance(value, ModuleType)
    }
    exported = {
        name for name in module.__all__ if not isinstance(getattr(module, name), ModuleType)
    }
    return public - exported, exported - public


@pytest.mark.parametrize("package", ["infra2_sdk", "infra2_sdk.runtime"])
def test_public_namespace_equals_all(package: str) -> None:
    """A leftover import line, such as `from x import Name`, must fail."""
    module = importlib.import_module(package)
    assert module.__all__, f"{package}.__all__ is empty: this test would check nothing"
    leftover, missing = _namespace_differences(module)
    assert not leftover, f"{package} exposes names that __all__ does not list: {sorted(leftover)}"
    assert not missing, f"{package}.__all__ lists names that are not attributes: {sorted(missing)}"


def test_the_namespace_check_rejects_a_leftover_import_and_a_missing_attribute() -> None:
    fake = ModuleType("fake")
    fake.__all__ = ["kept", "sub"]  # type: ignore[attr-defined]
    fake.kept = 1  # type: ignore[attr-defined]
    fake.sub = ModuleType("sub")  # type: ignore[attr-defined]
    fake._private = 1  # type: ignore[attr-defined]
    assert _namespace_differences(fake) == (set(), set())
    fake.leftover = object()  # type: ignore[attr-defined]
    assert _namespace_differences(fake) == ({"leftover"}, set())
    del fake.kept
    fake.__all__ = ["sub", "kept"]
    with pytest.raises(AttributeError):
        _namespace_differences(fake)  # a name in __all__ with no attribute fails loudly


def test_infra2_sdk_runtime_all_matches_actual_attributes() -> None:
    """Verify that every symbol declared in runtime.__all__ exists on infra2_sdk.runtime."""
    runtime_all = infra2_sdk.runtime.__all__
    assert len(runtime_all) == len(set(runtime_all)), "duplicate name in runtime.__all__"
    for name in runtime_all:
        assert hasattr(infra2_sdk.runtime, name), f"runtime.__all__ has {name!r} but missing"


def test_infra2_sdk_runtime_submodules_accessible() -> None:
    """Verify that all canonical runtime submodules are accessible on infra2_sdk.runtime."""
    expected_submodules = [
        "config_schema",
        "environment",
        "health",
        "http",
        "identity",
        "otel",
        "postgres",
        "s3",
    ]
    for sub in expected_submodules:
        assert hasattr(infra2_sdk.runtime, sub), f"infra2_sdk.runtime should expose {sub!r}"
        mod = getattr(infra2_sdk.runtime, sub)
        assert isinstance(mod, ModuleType), f"infra2_sdk.runtime.{sub} should be a module"


def test_import_isolation_without_optional_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify modules import cleanly without optional third-party packages installed."""
    blocked = {"boto3", "botocore", "psycopg", "httpx", "opentelemetry"}

    class BlockOptionalFinder:
        def find_spec(self, fullname: str, path: object = None, target: object = None) -> None:
            if fullname.split(".")[0] in blocked:
                raise ModuleNotFoundError(f"No module named {fullname!r} (blocked for testing)")
            return None

    monkeypatch.setattr(sys, "meta_path", [BlockOptionalFinder(), *sys.meta_path])

    from infra2_sdk.runtime.s3 import S3Settings, create_s3_client

    settings = S3Settings(bucket="my-bucket")
    with pytest.raises(RuntimeError, match="boto3 is required; install infra2-sdk\\[s3\\]"):
        create_s3_client(settings)


def test_infra2_sdk_unknown_attribute_raises() -> None:
    """Verify error on unknown attribute and removed symbols."""
    import importlib

    with pytest.raises(AttributeError, match="has no attribute 'nonexistent_symbol'"):
        _ = infra2_sdk.nonexistent_symbol
    with pytest.raises(AttributeError, match="has no attribute 'images'"):
        _ = infra2_sdk.images
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("infra2_sdk.images")


def test_consolidated_secondary_modules() -> None:
    """Verify that secondary modules provide their consolidated capabilities."""
    from infra2_sdk.deploy import dispatch_and_wait, poll_until_healthy
    from infra2_sdk.refs import resolve_image_digest
    from infra2_sdk.runtime.environment import RuntimeEnvKey, runtime_env_contract
    from infra2_sdk.runtime.health import DependencyManifest, check_health

    # Each capability is defined in its canonical module, not in a removed shim.
    assert dispatch_and_wait.__module__ == "infra2_sdk.deploy"
    assert poll_until_healthy.__module__ == "infra2_sdk.deploy"
    assert resolve_image_digest.__module__ == "infra2_sdk.refs"
    assert RuntimeEnvKey.__module__ == "infra2_sdk.runtime.environment"
    assert RuntimeEnvKey.ENVIRONMENT == "ENVIRONMENT"
    assert runtime_env_contract.__module__ == "infra2_sdk.runtime.environment"
    assert DependencyManifest.__module__ == "infra2_sdk.runtime.health"
    assert check_health.__module__ == "infra2_sdk.runtime.health"


# --- The exported surface is locked: every exported name has a recorded reason ----------------


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict:
    keys = [key for key, _ in pairs]
    for key in keys:
        if keys.count(key) > 1:
            raise ValueError(f"duplicate key {key!r}")
    return dict(pairs)


def _load_surface(text: str) -> dict:
    """Parse surface.json text. A duplicate key is an error: the later value would win."""
    return json.loads(text, object_pairs_hook=_reject_duplicate_keys)


SURFACE = _load_surface(Path(__file__).with_name("surface.json").read_text(encoding="utf-8"))
README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")
PACKAGES = ("infra2_sdk", "infra2_sdk.runtime")
REASON_PREFIXES = ("consumer:", "documented:", "held:")
CONSUMER_REPOSITORIES = frozenset({"infra2", "finance_report", "truealpha"})
# A hold is a recorded decision: name -> exact reason. A new hold needs a new line here and a
# review of that decision. surface.json and this table must agree on the names and the reasons.
HELD = {
    "RUNTIME_ENV_CONTRACT_VERSION": "held: issue #102 decision 7 (adopt, do not delete)",
    "RUNTIME_ENV_SPECS": "held: issue #102 decision 7 (adopt, do not delete)",
    "runtime_env_contract": "held: issue #102 decision 7 (adopt, do not delete)",
    "RuntimeIdentity": (
        "held: infra2 docs/ssot/core.environments.md identity decision (not adopted yet)"
    ),
}


def _readme_sections() -> dict[str, str]:
    """Title of each level 2 or level 3 heading of the README, mapped to its text."""
    parts = re.split(r"^#{2,3} (.+)$", README, flags=re.MULTILINE)
    titles = parts[1::2]
    assert len(titles) == len(set(titles)), "README has two headings with one title"
    return dict(zip(titles, parts[2::2], strict=True))


def _surface_entries() -> list[tuple[str, str, str]]:
    return [
        (package, name, reason) for package in PACKAGES for name, reason in SURFACE[package].items()
    ]


def test_the_surface_loader_rejects_a_duplicate_key() -> None:
    assert _load_surface('{"a": {"x": "1", "y": "2"}}') == {"a": {"x": "1", "y": "2"}}
    with pytest.raises(ValueError, match="duplicate key 'x'"):
        _load_surface('{"a": {"x": "1", "x": "2"}}')
    with pytest.raises(ValueError, match="duplicate key 'a'"):
        _load_surface('{"a": {}, "a": {}}')


def test_surface_lock_lists_exactly_the_two_packages() -> None:
    assert set(SURFACE) == set(PACKAGES)


@pytest.mark.parametrize("package", PACKAGES)
def test_exported_names_equal_the_surface_lock(package: str) -> None:
    exported = set(importlib.import_module(package).__all__)
    locked = set(SURFACE[package])
    assert exported, f"{package}.__all__ is empty: this test would check nothing"
    assert exported == locked, (
        f"{package}: exported without a reason in tests/surface.json: {sorted(exported - locked)}; "
        f"reason without an export: {sorted(locked - exported)}"
    )


def test_every_reason_starts_with_a_known_prefix_and_gives_a_detail() -> None:
    entries = _surface_entries()
    assert entries, "tests/surface.json lists no name: this test would check nothing"
    for package, name, reason in entries:
        prefixes = [prefix for prefix in REASON_PREFIXES if reason.startswith(prefix)]
        assert len(prefixes) == 1, f"{package}.{name}: reason {reason!r} has no known prefix"
        assert reason.removeprefix(prefixes[0]).strip(), f"{package}.{name}: empty reason detail"


def test_consumer_reasons_name_known_repositories() -> None:
    """Check the form of each `consumer:` reason, not its truth.

    The SDK repository cannot read the consumers. It cannot tell whether a named repository
    imports the name. The release check does that: consumer_import_check.py with --tests, and
    the census sdk_api_reach.py. Both belong to issue #102 and live in the infra2 workspace,
    in .agents/runs/env-inventory-20261013/tool. A false claim passes this test and fails
    that check.
    """
    checked = 0
    for package, name, reason in _surface_entries():
        if not reason.startswith("consumer:"):
            continue
        repositories = [part.strip() for part in reason.removeprefix("consumer:").split(",")]
        assert repositories == sorted(set(repositories)), f"{package}.{name}: {reason!r}"
        assert set(repositories) <= CONSUMER_REPOSITORIES, f"{package}.{name}: {reason!r}"
        checked += 1
    assert checked, "no consumer reason: this test would check nothing"


def test_documented_reasons_point_to_a_readme_section_that_names_the_export() -> None:
    sections = _readme_sections()
    checked = 0
    for package, name, reason in _surface_entries():
        if not reason.startswith("documented:"):
            continue
        match = re.fullmatch(r"documented: README section (?P<title>.+)", reason)
        assert match, f"{package}.{name}: write 'documented: README section <heading>'"
        assert match["title"] in sections, f"{package}.{name}: no README heading {match['title']!r}"
        is_module = isinstance(getattr(importlib.import_module(package), name), ModuleType)
        needle = f"{package}.{name}" if is_module else name
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])"
        assert re.search(pattern, sections[match["title"]]), (
            f"{package}.{name}: README section {match['title']!r} does not name {needle!r}"
        )
        checked += 1
    assert checked, "no documented reason: this test would check nothing"


def test_held_names_and_reasons_equal_the_recorded_table() -> None:
    held = [(name, reason) for _, name, reason in _surface_entries() if reason.startswith("held:")]
    assert held, "no held reason: this test would check nothing"
    by_name = dict(held)
    assert len(by_name) == len(held), "one name is held in both packages"
    assert set(by_name) == set(HELD), (
        f"held in surface.json only: {sorted(set(by_name) - set(HELD))}; "
        f"held in HELD only: {sorted(set(HELD) - set(by_name))}"
    )
    for name, reason in by_name.items():
        assert reason == HELD[name], f"{name}: the reason {reason!r} is not {HELD[name]!r}"
