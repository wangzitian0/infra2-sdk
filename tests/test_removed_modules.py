"""Guards for compatibility modules removed in 3.3.0 (issue #90).

Each removed module must stay unimportable. A new shim with the same name fails these tests.
"""

from __future__ import annotations

import importlib

import pytest

# The frozen list lives in wheel_smoke.py, so the source tests and the wheel smoke test share it.
# Do not derive it from the source tree: a derived list would shrink with the tree.
from wheel_smoke import REMOVED_MODULES

import infra2_sdk
import infra2_sdk.runtime


def test_removed_module_list_has_six_unique_names() -> None:
    assert len(REMOVED_MODULES) == 6
    assert len(set(REMOVED_MODULES)) == 6


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_removed_module_raises_module_not_found(module: str) -> None:
    importlib.invalidate_caches()
    with pytest.raises(ModuleNotFoundError) as excinfo:
        importlib.import_module(module)
    # The missing module must be the removed one, not a broken import inside another module.
    assert excinfo.value.name == module


@pytest.mark.parametrize("module", REMOVED_MODULES)
def test_removed_module_is_not_an_attribute_or_export_of_its_package(module: str) -> None:
    parent_name, _, short_name = module.rpartition(".")
    parent = {"infra2_sdk": infra2_sdk, "infra2_sdk.runtime": infra2_sdk.runtime}[parent_name]
    assert not hasattr(parent, short_name)
    assert short_name not in parent.__all__
