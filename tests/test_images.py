"""Verification that deprecated images module is completely eradicated in SDK 3.0."""

from __future__ import annotations

import importlib

import pytest

import infra2_sdk


def test_images_module_removed_in_v3() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("infra2_sdk.images")
    assert "images" not in infra2_sdk.__all__
    assert not hasattr(infra2_sdk, "images")
