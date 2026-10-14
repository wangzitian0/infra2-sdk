"""Guards for names and environment variables removed in 3.4.0 (issue #102).

Each removed name must stay absent. A new shim with the same name fails these tests.
"""

from __future__ import annotations

import importlib

import pytest

# The frozen lists live in wheel_smoke.py, so the source tests and the wheel smoke test share them.
# Do not derive them from the source tree: a derived list would shrink with the tree.
from wheel_smoke import REMOVED_ENV_MEMBERS, REMOVED_ENV_NAMES, REMOVED_NAMES

import infra2_sdk
import infra2_sdk.runtime
from infra2_sdk.runtime.environment import RuntimeEnvKey, runtime_env_contract

# Second, independent copies. They are the oracle for the lists imported above.
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
        "infra2_sdk.runtime.http:HttpClientSettings.from_env",
        "infra2_sdk.runtime.postgres:PostgresSettings.from_env",
        "infra2_sdk.runtime.s3:S3Settings.from_env",
        "infra2_sdk.capacity:OnePasswordCapacityReport",
        "infra2_sdk.capacity:Runner",
        "infra2_sdk.refs:ReleaseIdentity",
        "infra2_sdk.routing:DokployDomainSpec",
        "infra2_sdk.runtime.environment:env_bool",
        "infra2_sdk.runtime.environment:env_float",
        "infra2_sdk.runtime.health:DependencyUnavailableError",
        "infra2_sdk.secrets:ReadOnlyBackendError",
    ]
)
EXPECTED_REMOVED_ENV_MEMBERS = frozenset(
    [
        "HTTP_TIMEOUT_SECONDS",
        "HTTP_CONNECT_TIMEOUT_SECONDS",
        "HTTP_MAX_CONNECTIONS",
        "HTTP_MAX_KEEPALIVE_CONNECTIONS",
        "HTTP_CONNECT_RETRIES",
        "HTTP_USER_AGENT",
        "HTTP_FOLLOW_REDIRECTS",
        "OBJECT_STORAGE_PROTOCOL",
        "S3_BUCKET",
        "S3_ENDPOINT_URL",
        "S3_REGION",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "S3_CONNECT_TIMEOUT_SECONDS",
        "S3_READ_TIMEOUT_SECONDS",
        "S3_ADDRESSING_STYLE",
        "DATABASE_URL",
        "DATABASE_CONNECT_TIMEOUT_SECONDS",
        "DATABASE_STATEMENT_TIMEOUT_SECONDS",
    ]
)
EXPECTED_REMOVED_ENV_NAMES = frozenset(
    [
        "HTTP_TIMEOUT_SECONDS",
        "HTTP_CONNECT_TIMEOUT_SECONDS",
        "HTTP_MAX_CONNECTIONS",
        "HTTP_MAX_KEEPALIVE_CONNECTIONS",
        "HTTP_CONNECT_RETRIES",
        "HTTP_USER_AGENT",
        "HTTP_FOLLOW_REDIRECTS",
        "OBJECT_STORAGE_PROTOCOL",
        "OBJECT_STORAGE_DRIVER",
        "S3_BUCKET",
        "AWS_ENDPOINT_URL_S3",
        "S3_ENDPOINT_URL",
        "S3_ENDPOINT",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "S3_REGION",
        "AWS_ACCESS_KEY_ID",
        "S3_ACCESS_KEY",
        "AWS_SECRET_ACCESS_KEY",
        "S3_SECRET_KEY",
        "AWS_SESSION_TOKEN",
        "S3_SESSION_TOKEN",
        "S3_CONNECT_TIMEOUT_SECONDS",
        "S3_READ_TIMEOUT_SECONDS",
        "S3_ADDRESSING_STYLE",
        "DATABASE_URL",
        "DATABASE_CONNECT_TIMEOUT_SECONDS",
        "DATABASE_STATEMENT_TIMEOUT_SECONDS",
        "SERVICE_NAME",
    ]
)

_NAME_IDS = [f"{module}.{name}" for module, name in REMOVED_NAMES]
_UNDOTTED = [pair for pair in REMOVED_NAMES if "." not in pair[1]]


def test_removed_name_lists_have_unique_entries() -> None:
    assert len(REMOVED_NAMES) == len(set(REMOVED_NAMES)) == 29
    assert len(REMOVED_ENV_MEMBERS) == len(set(REMOVED_ENV_MEMBERS)) == 20
    assert len(REMOVED_ENV_NAMES) == len(set(REMOVED_ENV_NAMES)) == 29


def test_removed_name_lists_equal_the_independent_literal_sets() -> None:
    assert {f"{module}:{name}" for module, name in REMOVED_NAMES} == EXPECTED_REMOVED_NAMES
    assert set(REMOVED_ENV_MEMBERS) == EXPECTED_REMOVED_ENV_MEMBERS
    assert set(REMOVED_ENV_NAMES) == EXPECTED_REMOVED_ENV_NAMES


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


@pytest.mark.parametrize("member", REMOVED_ENV_MEMBERS)
def test_removed_environment_member_is_not_in_runtime_env_key(member: str) -> None:
    assert member not in RuntimeEnvKey.__members__
    assert not hasattr(RuntimeEnvKey, member)


@pytest.mark.parametrize("name", REMOVED_ENV_NAMES)
def test_removed_environment_name_is_not_in_the_contract(name: str) -> None:
    variables = runtime_env_contract()["variables"]
    assert variables, "the contract lists no variable: this test would check nothing"
    canonical = {item["name"] for item in variables}
    aliases = {alias for item in variables for alias in item["aliases"]}
    assert name not in canonical, f"{name} is still a canonical name"
    assert name not in aliases, f"{name} is still an alias"
    assert name not in {key.value for key in RuntimeEnvKey}, f"{name} is still a key value"
