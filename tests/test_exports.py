"""Tests for canonical package exports, __all__ declarations, and isolation."""

from __future__ import annotations

import sys
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
        "snapshot",
        "transport",
    ]
    for sub in expected_submodules:
        assert hasattr(infra2_sdk, sub), f"infra2_sdk should expose submodule {sub!r}"
        mod = getattr(infra2_sdk, sub)
        assert isinstance(mod, ModuleType), f"infra2_sdk.{sub} should be a module"


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
    from infra2_sdk.refs import ReleaseIdentity, resolve_release_identity
    from infra2_sdk.runtime.environment import RuntimeEnvKey, runtime_env_contract
    from infra2_sdk.runtime.health import DependencyManifest, check_health

    # Each capability is defined in its canonical module, not in a removed shim.
    assert dispatch_and_wait.__module__ == "infra2_sdk.deploy"
    assert poll_until_healthy.__module__ == "infra2_sdk.deploy"
    assert ReleaseIdentity.__module__ == "infra2_sdk.refs"
    assert resolve_release_identity.__module__ == "infra2_sdk.refs"
    assert RuntimeEnvKey.__module__ == "infra2_sdk.runtime.environment"
    assert RuntimeEnvKey.ENVIRONMENT == "ENVIRONMENT"
    assert runtime_env_contract.__module__ == "infra2_sdk.runtime.environment"
    assert DependencyManifest.__module__ == "infra2_sdk.runtime.health"
    assert check_health.__module__ == "infra2_sdk.runtime.health"
