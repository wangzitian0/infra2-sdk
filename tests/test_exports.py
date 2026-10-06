"""Tests for canonical package exports, __all__ declarations, and isolation."""

from __future__ import annotations

import sys
from types import ModuleType

import pytest

import infra2_sdk
import infra2_sdk.runtime


def test_infra2_sdk_root_all_matches_actual_attributes() -> None:
    """Verify that every symbol declared in __all__ exists on infra2_sdk."""
    for name in infra2_sdk.__all__:
        assert hasattr(infra2_sdk, name), f"infra2_sdk.__all__ has {name!r} but missing"
        val = getattr(infra2_sdk, name)
        assert val is not None, f"infra2_sdk.{name} should not be None"


def test_infra2_sdk_submodules_accessible_from_root() -> None:
    """Verify that all canonical non-private submodules are accessible on infra2_sdk."""
    expected_submodules = [
        "capacity",
        "ci",
        "delivery",
        "deploy",
        "deploy_health",
        "dispatch",
        "images",
        "manifests",
        "refs",
        "release",
        "routing",
        "rules",
        "runtime",
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
    for name in infra2_sdk.runtime.__all__:
        assert hasattr(infra2_sdk.runtime, name), f"runtime.__all__ has {name!r} but missing"
        val = getattr(infra2_sdk.runtime, name)
        assert val is not None, f"infra2_sdk.runtime.{name} should not be None"


def test_infra2_sdk_runtime_submodules_accessible() -> None:
    """Verify that all canonical runtime submodules are accessible on infra2_sdk.runtime."""
    expected_submodules = [
        "config_schema",
        "dependencies",
        "environ",
        "environment",
        "health",
        "http",
        "identity",
        "otel",
        "postgres",
        "probes",
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


def test_infra2_sdk_getattr_images_and_unknown() -> None:
    """Verify PEP 562 lazy loading of images and error on unknown attribute."""
    assert infra2_sdk.images is not None
    with pytest.raises(AttributeError, match="has no attribute 'nonexistent_symbol'"):
        _ = infra2_sdk.nonexistent_symbol
