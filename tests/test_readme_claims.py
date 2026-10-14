"""Tests that run the claims of the 3.4.0 compatibility notes in the README (issue #102).

A reader follows the README advice without the maintainers. Each test runs one claim, so that
the advice stays true: the wrapper that replaces a removed check, the behavior without
SERVICE_NAME, the error of a string lookup, the names that left the packages, and the counts.
"""

from __future__ import annotations

import asyncio
import importlib
import re
import textwrap
from pathlib import Path

import pytest

# The frozen lists live in wheel_smoke.py. FIXED_READS is the checked list of fixed reads.
from test_source_environment_reads import FIXED_READS
from wheel_smoke import REMOVED_ENV_MEMBERS, REMOVED_ENV_NAMES

import infra2_sdk.runtime
from infra2_sdk.runtime import DependencyStatus, probe_postgres, probe_s3, run_probes
from infra2_sdk.runtime.environment import (
    RuntimeEnvKey,
    resolve_runtime_env,
    runtime_env_contract,
    runtime_env_spec,
)
from infra2_sdk.runtime.identity import RuntimeIdentity
from infra2_sdk.runtime.otel import OtelSettings
from infra2_sdk.runtime.postgres import PostgresSettings
from infra2_sdk.runtime.s3 import S3Settings

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(encoding="utf-8")

# --- The wrapper that replaces PostgresCheck and S3Check ---------------------------------------


class _Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, _query):
        return self

    def fetchone(self):
        return (1,)


class _S3Client:
    def head_bucket(self, **kwargs):
        return kwargs


def _readme_wrapper_source() -> str:
    """The Python block of the README that defines DatabaseCheck, without its indentation."""
    blocks = re.findall(r"^[ \t]*```python\n(.*?)^[ \t]*```", README, flags=re.S | re.M)
    wrappers = [textwrap.dedent(block) for block in blocks if "class DatabaseCheck" in block]
    assert len(wrappers) == 1, "the README needs exactly one block that defines DatabaseCheck"
    return wrappers[0]


def _run_the_wrappers(monkeypatch: pytest.MonkeyPatch, source: str):
    """Run the README source with the real probes and fake connections. No network is used."""
    real_postgres, real_s3 = probe_postgres, probe_s3
    monkeypatch.setattr(
        infra2_sdk.runtime,
        "probe_postgres",
        lambda settings: real_postgres(settings, connector=lambda *_a, **_k: _Connection()),
    )
    monkeypatch.setattr(
        infra2_sdk.runtime,
        "probe_s3",
        lambda settings: real_s3(settings, client=_S3Client()),
    )
    namespace = {
        "db_settings": PostgresSettings(dsn="postgresql://user:pass@db.invalid/app"),
        "storage_settings": S3Settings(bucket="bucket"),
    }
    exec(compile(source, "README.md", "exec"), namespace)
    checks = [namespace["DatabaseCheck"](), namespace["ObjectStorageCheck"]()]
    return asyncio.run(run_probes(checks))


def test_the_readme_wrappers_pass_run_probes_with_the_names_of_the_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    results = _run_the_wrappers(monkeypatch, _readme_wrapper_source())

    assert [(result.name, result.status) for result in results] == [
        ("database", DependencyStatus.PRESENT),
        ("object_storage", DependencyStatus.PRESENT),
    ]


def test_run_probes_reports_a_wrapper_with_another_name_as_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _readme_wrapper_source()
    assert 'name = "database"' in source
    renamed = source.replace('name = "database"', 'name = "db"')

    database, storage = _run_the_wrappers(monkeypatch, renamed)

    assert (database.name, database.status) == ("db", DependencyStatus.ABSENT)
    assert "probe result name does not match check name" in database.detail
    assert storage.status is DependencyStatus.PRESENT


# --- A deployment that sets only SERVICE_NAME --------------------------------------------------

_IDENTITY = {"ENVIRONMENT": "staging", "SERVICE_VERSION": "v1", "GIT_COMMIT_SHA": "a" * 40}
_ENDPOINT = {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"}


def test_a_deployment_with_only_service_name_gets_the_documented_behavior() -> None:
    only = {"SERVICE_NAME": "legacy"}
    assert OtelSettings.from_env({**_ENDPOINT, **only}).service_name == "unknown_service"
    with pytest.raises(ValueError, match="OTEL_SERVICE_NAME is required"):
        RuntimeIdentity.from_env({**_IDENTITY, **only})


def test_the_value_of_otel_service_name_wins_and_no_conflict_is_reported() -> None:
    both = {"SERVICE_NAME": "legacy", "OTEL_SERVICE_NAME": "current"}
    assert OtelSettings.from_env({**_ENDPOINT, **both}).service_name == "current"
    assert RuntimeIdentity.from_env({**_IDENTITY, **both}).service_name == "current"


# --- A string lookup of a removed variable -----------------------------------------------------

_LOOKUPS = {
    "resolve_runtime_env": lambda name: resolve_runtime_env({name: "value"}, name),
    "runtime_env_spec": runtime_env_spec,
    "RuntimeEnvKey": RuntimeEnvKey,
}


@pytest.mark.parametrize("lookup", sorted(_LOOKUPS))
@pytest.mark.parametrize("name", REMOVED_ENV_NAMES)
def test_a_string_lookup_of_a_removed_variable_raises_value_error(lookup: str, name: str) -> None:
    with pytest.raises(ValueError, match="is not a valid RuntimeEnvKey"):
        _LOOKUPS[lookup](name)


@pytest.mark.parametrize("lookup", sorted(_LOOKUPS))
def test_a_string_lookup_of_a_current_variable_still_works(lookup: str) -> None:
    assert _LOOKUPS[lookup]("ENVIRONMENT") is not None


# --- The names that left the packages ----------------------------------------------------------

# Independent oracle: base (3.3.0) __all__ minus the current __all__, for the names that stay
# defined in their module. Names deleted outright are in REMOVED_NAMES.
LEFT_THE_PACKAGES = {
    "infra2_sdk": {
        "capacity": ["CapacityLimit"],
        "ci": ["validate_gate"],
        "delivery": ["BudgetStatus", "DisagreementKind", "PipelineStage", "validate_stage_result"],
        "deploy": [
            "CONTRACT_VERSION",
            "DeployEvidence",
            "DeployState",
            "HealthCheckResult",
            "ProductionEvidencePolicy",
            "build_deploy_request",
            "derive_release_evidence",
            "fetch_production_evidence_policy",
        ],
        "routing": ["DEFAULT_BASE_DOMAIN", "RouteEndpoint"],
        "secrets": ["SecretsBackend", "TokenStatus"],
    },
    "infra2_sdk.runtime": {
        "config_schema": ["EnvironmentValidation", "validate_environment"],
        "environment": [
            "APP_OWNED_TIERS",
            "EnvironmentConflictError",
            "PLATFORM_OWNED_TIERS",
            "ResolvedEnvValue",
            "RuntimeEnvSpec",
            "UnknownEnvironmentPolicy",
            "normalize_deployment_environment",
            "resolve_env",
            "strict_environment_from_env",
        ],
        "health": ["HealthStatus", "health_response"],
    },
}
_LEFT = [
    (package, module, name)
    for package, modules in LEFT_THE_PACKAGES.items()
    for module, names in modules.items()
    for name in names
]


def _readme_left_the_packages() -> tuple[int, dict[str, dict[str, list[str]]]]:
    """The count and the grouped names of the README bullet about the names that left."""
    match = re.search(
        r"^- In v3\.4\.0, (\d+) names left `__all__`.*?(?=^- )", README, flags=re.S | re.M
    )
    assert match, "the README has no bullet about the names that left __all__"
    text = match.group(0).replace("\n      ", " ")  # join the continuation lines
    grouped: dict[str, dict[str, list[str]]] = {}
    package = ""
    for line in text.splitlines():
        if found := re.fullmatch(r"  - in `([\w.]+)`:", line):
            package = found.group(1)
            grouped[package] = {}
        elif found := re.fullmatch(r"    - `(\w+)`: (.*)", line):
            grouped[package][found.group(1)] = re.findall(r"`(\w+)`", found.group(2))
    return int(match.group(1)), grouped


def test_the_readme_lists_the_names_that_left_the_packages_by_module() -> None:
    count, grouped = _readme_left_the_packages()

    assert grouped == LEFT_THE_PACKAGES
    assert count == len(_LEFT) == 31


@pytest.mark.parametrize(
    ("package", "module", "name"), _LEFT, ids=[f"{m}.{n}" for _, m, n in _LEFT]
)
def test_a_name_that_left_stays_in_its_module_and_is_not_in_the_package(
    package: str, module: str, name: str
) -> None:
    owner = importlib.import_module(f"{package}.{module}")
    root = importlib.import_module(package)

    assert name in vars(owner), f"{package}.{module}.{name} is gone"
    assert not hasattr(root, name), f"{package}.{name} is still an attribute"
    assert name not in root.__all__, f"{package}.__all__ still lists {name}"


# --- The counts that the README states ---------------------------------------------------------


def test_the_readme_counts_the_fixed_names_and_the_variables_of_the_contract() -> None:
    fixed = re.search(r"(\w+) more fixed names exist outside\s+the table\.", README)
    assert fixed and fixed.group(1) == "Five"
    assert len(FIXED_READS) == 5
    paragraph = README[fixed.start() : fixed.start() + 500]
    for name in FIXED_READS:
        assert f"`{name}`" in paragraph, f"the README paragraph does not name {name}"

    version_note = re.search(r"Version 2 lists (\d+) variables", README)
    assert version_note and int(version_note.group(1)) == len(runtime_env_contract()["variables"])
    lost = re.search(r"lost the (\d+) variables above", README)
    assert lost and int(lost.group(1)) == len(REMOVED_ENV_MEMBERS) == 20
