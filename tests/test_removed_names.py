"""Guards for names removed in 3.4.0 (issue #102).

Each removed name must stay absent. A new shim with the same name fails these tests.
"""

from __future__ import annotations

import importlib

import pytest

# The frozen lists live in wheel_smoke.py, so the source tests and the wheel smoke test share them.
# Do not derive them from the source tree: a derived list would shrink with the tree.
from wheel_smoke import REMOVED_NAMES

import infra2_sdk
import infra2_sdk.runtime

# Second, independent copy. It is the oracle for the list imported above.
EXPECTED_REMOVED_NAMES = frozenset(
    [
        "infra2_sdk.capacity:LEVELS",
        "infra2_sdk.capacity:onepassword_capacity",
        "infra2_sdk.delivery:PREVIEW_RELEVANT_STAGES",
        "infra2_sdk.refs:resolve_release_identity",
        "infra2_sdk.refs:verify_runtime_identity",
        "infra2_sdk.routing:resolve_dokploy_domains",
        "infra2_sdk.runtime.config_schema:settings_json_schema",
        "infra2_sdk.runtime.health:assert_required_dependencies",
        "infra2_sdk.runtime.http:retryable_request",
        "infra2_sdk.runtime.http:parse_retry_after",
        "infra2_sdk.runtime.http:HttpCheck",
        "infra2_sdk.runtime.identity:runtime_identity_fingerprint",
        "infra2_sdk.runtime.otel:inject_trace_context",
        "infra2_sdk.runtime.otel:extract_trace_context",
        "infra2_sdk.runtime.postgres:PostgresCheck",
        "infra2_sdk.runtime.s3:S3Check",
        "infra2_sdk.secrets:EnvBackend",
        "infra2_sdk.snapshot:verify_snapshot_artifact",
        "infra2_sdk.capacity:OnePasswordCapacityReport",
        "infra2_sdk.routing:DokployDomainSpec",
        "infra2_sdk.runtime.health:DependencyUnavailableError",
        "infra2_sdk.secrets:ReadOnlyBackendError",
    ]
)

_NAME_IDS = [f"{module}.{name}" for module, name in REMOVED_NAMES]
_UNDOTTED = [pair for pair in REMOVED_NAMES if "." not in pair[1]]


def test_removed_name_list_has_unique_entries() -> None:
    assert len(REMOVED_NAMES) == len(set(REMOVED_NAMES)) == 22


def test_removed_name_list_equals_the_independent_literal_set() -> None:
    assert {f"{module}:{name}" for module, name in REMOVED_NAMES} == EXPECTED_REMOVED_NAMES


@pytest.mark.parametrize(("module", "dotted"), REMOVED_NAMES, ids=_NAME_IDS)
def test_removed_name_is_absent_while_its_owner_still_exists(module: str, dotted: str) -> None:
    owner = importlib.import_module(module)  # the module must still import
    *parents, leaf = dotted.split(".")
    for part in parents:
        owner = getattr(owner, part)  # the class of a removed method must still exist
    assert not hasattr(owner, leaf), f"{module}.{dotted} was removed in 3.4.0"


@pytest.mark.parametrize(("module", "name"), _UNDOTTED, ids=[f"{m}.{n}" for m, n in _UNDOTTED])
def test_removed_name_is_not_exported_by_a_package(module: str, name: str) -> None:
    for package in (infra2_sdk, infra2_sdk.runtime):
        assert name not in package.__all__, f"{package.__name__}.__all__ lists {name}"
        assert not hasattr(package, name), f"{package.__name__} still exposes {name}"
