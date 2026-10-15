"""Guards for names and environment variables removed in 3.4.0 (issue #102).

A removed name must stay absent from its old module, from the two packages, and from every
other module of the package. A shim that gives the name back under a new owner fails.
A removed environment variable must stay out of runtime_env_contract().

These tests do not check:

- a renamed copy of a removed object, because the name differs;
- a name that a module creates while it runs, for example in __getattr__;
- a name bound only inside a function or a class body;
- a dotted entry (a removed method) anywhere but on its old class.
"""

from __future__ import annotations

import importlib
import json
import pkgutil
from pathlib import Path
from types import ModuleType

import pytest
import wheel_smoke

# The frozen lists live in wheel_smoke.py, so the source tests and the wheel smoke test share them.
# Do not derive them from the source tree: a derived list would shrink with the tree.
from wheel_smoke import REMOVED_ENV_MEMBERS, REMOVED_ENV_NAMES, REMOVED_MODULES, REMOVED_NAMES

import infra2_sdk
import infra2_sdk.runtime
from infra2_sdk.runtime.environment import RuntimeEnvKey, runtime_env_contract

# Second, independent copies. They are the oracle for the lists imported above.
EXPECTED_REMOVED_NAMES = frozenset(
    [
        "infra2_sdk.capacity:LEVELS",
        "infra2_sdk.capacity:onepassword_capacity",
        "infra2_sdk.delivery:PREVIEW_RELEVANT_STAGES",
        "infra2_sdk.delivery:acceleration_allowed",
        "infra2_sdk.delivery:detect_disagreement",
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
        "infra2_sdk.deploy:DeployState",
        "infra2_sdk.deploy:DeployStatus",
        "infra2_sdk.snapshot:AnonymizedSnapshotManifest",
        "infra2_sdk.snapshot:ResidualScanProof",
        "infra2_sdk.snapshot:ResidualScanStatus",
        "infra2_sdk.snapshot:SNAPSHOT_MANIFEST_VERSION",
        "infra2_sdk.snapshot:SnapshotArtifact",
        "infra2_sdk.snapshot:SnapshotArtifactFormat",
        "infra2_sdk.snapshot:SnapshotProducer",
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

_UNDOTTED = [pair for pair in REMOVED_NAMES if "." not in pair[1]]
_REMOVED_SIMPLE_NAMES = frozenset(name for _, name in _UNDOTTED)
# A removed simple name that one module may bind on purpose: (module, name) -> reason.
# A new entry needs a review of its reason.
ALLOWED_REUSE: dict[tuple[str, str], str] = {
    ("infra2_sdk.secrets", "Runner"): (
        "secrets.py defines its own type alias for subprocess.run, unrelated to the "
        "removed alias capacity.Runner"
    ),
}
# A removed module has no owner to look into. Its names are checked by the other tests.
_LIVE_OWNER = [pair for pair in REMOVED_NAMES if pair[0] not in REMOVED_MODULES]


def test_removed_name_lists_have_unique_entries() -> None:
    assert len(REMOVED_NAMES) == len(set(REMOVED_NAMES)) == 40
    assert len(REMOVED_ENV_MEMBERS) == len(set(REMOVED_ENV_MEMBERS)) == 20
    assert len(REMOVED_ENV_NAMES) == len(set(REMOVED_ENV_NAMES)) == 29


def test_removed_name_lists_equal_the_independent_literal_sets() -> None:
    assert {f"{module}:{name}" for module, name in REMOVED_NAMES} == EXPECTED_REMOVED_NAMES
    assert set(REMOVED_ENV_MEMBERS) == EXPECTED_REMOVED_ENV_MEMBERS
    assert set(REMOVED_ENV_NAMES) == EXPECTED_REMOVED_ENV_NAMES


@pytest.mark.parametrize(
    ("module", "dotted"), _LIVE_OWNER, ids=[f"{m}.{n}" for m, n in _LIVE_OWNER]
)
def test_removed_name_is_absent_while_its_owner_still_exists(module: str, dotted: str) -> None:
    owner = importlib.import_module(module)  # the module must still import
    *parents, leaf = dotted.split(".")
    for part in parents:
        owner = getattr(owner, part)  # the class of a removed method must still exist
    assert not hasattr(owner, leaf), f"{module}.{dotted} was removed"


@pytest.mark.parametrize(("module", "name"), _UNDOTTED, ids=[f"{m}.{n}" for m, n in _UNDOTTED])
def test_removed_name_is_not_exported_by_a_package(module: str, name: str) -> None:
    for package in (infra2_sdk, infra2_sdk.runtime):
        assert name not in package.__all__, f"{package.__name__}.__all__ lists {name}"
        assert not hasattr(package, name), f"{package.__name__} still exposes {name}"


@pytest.mark.parametrize("member", REMOVED_ENV_MEMBERS)
def test_removed_environment_member_is_not_in_runtime_env_key(member: str) -> None:
    assert member not in RuntimeEnvKey.__members__
    assert not hasattr(RuntimeEnvKey, member)


def _package_modules() -> list[ModuleType]:
    """The root package and every module below it, imported."""
    modules = [infra2_sdk]
    for info in pkgutil.walk_packages(infra2_sdk.__path__, prefix="infra2_sdk."):
        modules.append(importlib.import_module(info.name))
    return modules


def _module_names_on_disk() -> set[str]:
    root = Path(infra2_sdk.__file__).resolve().parent
    names = set()
    for path in root.rglob("*.py"):
        parts = path.relative_to(root.parent).with_suffix("").parts
        names.add(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    return names


def _bound_removed_names(
    module: ModuleType, removed: frozenset[str], allowed: dict[tuple[str, str], str]
) -> list[str]:
    """Public names of the module, and names in its __all__, that are removed names."""
    public = {name for name in vars(module) if not name.startswith("_")}
    listed = set(getattr(module, "__all__", ()))
    return sorted(
        name for name in (public | listed) & removed if (module.__name__, name) not in allowed
    )


def test_the_walk_reaches_every_module_on_disk() -> None:
    walked = {module.__name__ for module in _package_modules()}
    assert walked == _module_names_on_disk()
    assert len(walked) > 20, "the walk found almost no module: the next test would check nothing"


def test_no_module_binds_a_removed_simple_name() -> None:
    offenders = {
        module.__name__: found
        for module in _package_modules()
        if (found := _bound_removed_names(module, _REMOVED_SIMPLE_NAMES, ALLOWED_REUSE))
    }
    assert not offenders, f"a removed name came back: {offenders}"


def test_the_removed_name_check_rejects_a_shim_and_honors_the_allow_list() -> None:
    shim = ModuleType("infra2_sdk.shim")
    shim.PostgresCheck = object  # type: ignore[attr-defined]
    shim.__all__ = ["HttpCheck", "kept"]  # type: ignore[attr-defined]
    shim.kept = 1  # type: ignore[attr-defined]
    shim._LEVELS = 1  # type: ignore[attr-defined]  # private: not a public name
    removed = frozenset({"PostgresCheck", "HttpCheck", "LEVELS", "_LEVELS"})
    assert _bound_removed_names(shim, removed, {}) == ["HttpCheck", "PostgresCheck"]
    allowed = {("infra2_sdk.shim", "PostgresCheck"): "reason"}
    assert _bound_removed_names(shim, removed, allowed) == ["HttpCheck"]
    other_module = {("infra2_sdk.other", "PostgresCheck"): "reason"}
    assert _bound_removed_names(shim, removed, other_module) == ["HttpCheck", "PostgresCheck"]


def _stale_allowed_reuse(
    allowed: dict[tuple[str, str], str], modules: dict[str, ModuleType]
) -> list[str]:
    """Problems of the entries of the allow-list. An entry that no module needs is stale."""
    problems = []
    for (module_name, name), reason in allowed.items():
        if name not in _REMOVED_SIMPLE_NAMES:
            problems.append(f"{name} is not a removed name")
        module = modules.get(module_name)
        if module is None or name not in vars(module):
            problems.append(f"{module_name}.{name} is gone: remove the entry")
        if not reason.strip():
            problems.append(f"{module_name}.{name}: the entry has no reason")
    return problems


def test_every_allowed_reuse_is_still_needed() -> None:
    # An empty allow-list is the state to aim for. Then the check has no entry to read.
    modules = {module.__name__: module for module in _package_modules()}
    assert _stale_allowed_reuse(ALLOWED_REUSE, modules) == []


def test_the_stale_reuse_check_reports_each_kind_of_problem() -> None:
    shim = ModuleType("infra2_sdk.shim")
    shim.PostgresCheck = object  # type: ignore[attr-defined]
    modules = {"infra2_sdk.shim": shim}
    allowed = {
        ("infra2_sdk.shim", "PostgresCheck"): "reason",
        ("infra2_sdk.shim", "HttpCheck"): "the name is gone",
        ("infra2_sdk.gone", "PostgresCheck"): "the module is gone",
        ("infra2_sdk.shim", "kept"): "not a removed name",
        ("infra2_sdk.shim", "LEVELS"): " ",
    }
    assert _stale_allowed_reuse(allowed, modules) == [
        "infra2_sdk.shim.HttpCheck is gone: remove the entry",
        "infra2_sdk.gone.PostgresCheck is gone: remove the entry",
        "kept is not a removed name",
        "infra2_sdk.shim.kept is gone: remove the entry",
        "infra2_sdk.shim.LEVELS is gone: remove the entry",
        "infra2_sdk.shim.LEVELS: the entry has no reason",
    ]
    assert _stale_allowed_reuse({}, modules) == []


def test_the_removed_simple_names_are_the_undotted_entries() -> None:
    assert _REMOVED_SIMPLE_NAMES, "no removed simple name: the module check would check nothing"
    assert all("." not in name for name in _REMOVED_SIMPLE_NAMES)
    assert len(_REMOVED_SIMPLE_NAMES) == 37


def _strings(value: object):
    """Every string of a JSON document: keys and values, at any depth."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _strings(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def test_serialized_contract_names_no_removed_variable_anywhere() -> None:
    document = json.loads(json.dumps(runtime_env_contract()))
    found = set(_strings(document))
    assert "ENVIRONMENT" in found, "the walk found no variable: this test would check nothing"
    assert not found & set(REMOVED_ENV_NAMES), sorted(found & set(REMOVED_ENV_NAMES))


@pytest.mark.parametrize("name", REMOVED_ENV_NAMES)
def test_removed_environment_name_is_not_in_the_contract(name: str) -> None:
    variables = runtime_env_contract()["variables"]
    assert variables, "the contract lists no variable: this test would check nothing"
    canonical = {item["name"] for item in variables}
    aliases = {alias for item in variables for alias in item["aliases"]}
    assert name not in canonical, f"{name} is still a canonical name"
    assert name not in aliases, f"{name} is still an alias"
    assert name not in {key.value for key in RuntimeEnvKey}, f"{name} is still a key value"


@pytest.mark.parametrize(
    ("smoke", "attribute", "expected"),
    [
        ("smoke_package_data", "REMOVED_MODULES", 8),
        ("smoke_removed_names", "REMOVED_NAMES", 40),
        ("smoke_removed_names", "REMOVED_ENV_MEMBERS", 20),
        ("smoke_removed_names", "REMOVED_ENV_NAMES", 29),
    ],
)
def test_the_wheel_smoke_names_the_list_that_changed_size(
    monkeypatch: pytest.MonkeyPatch, smoke: str, attribute: str, expected: int
) -> None:
    shorter = getattr(wheel_smoke, attribute)[:-1]
    monkeypatch.setattr(wheel_smoke, attribute, shorter)
    with pytest.raises(AssertionError, match=rf"{attribute} has {expected - 1} entries"):
        getattr(wheel_smoke, smoke)()
