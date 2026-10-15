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
# defined in their module. Names deleted outright are in REMOVED_NAMES or REMOVED_IN_3_5_1.
LEFT_THE_PACKAGES = {
    "infra2_sdk": {
        "capacity": ["CapacityLimit"],
        "ci": ["validate_gate"],
        "delivery": ["BudgetStatus", "DisagreementKind", "PipelineStage", "validate_stage_result"],
        "deploy": [
            "CONTRACT_VERSION",
            "DeployEvidence",
            "HealthCheckResult",
            "ProductionEvidencePolicy",
            "fetch_production_evidence_policy",
        ],
        "routing": ["DEFAULT_BASE_DOMAIN"],
        "secrets": ["SecretsBackend", "TokenStatus"],
    },
    "infra2_sdk.runtime": {
        "config_schema": ["EnvironmentValidation", "validate_environment"],
        "environment": [
            "EnvironmentConflictError",
            "PLATFORM_OWNED_TIERS",
            "ResolvedEnvValue",
            "RuntimeEnvSpec",
            "UnknownEnvironmentPolicy",
            "normalize_deployment_environment",
            "resolve_env",
            "strict_environment_from_env",
        ],
        "health": ["HealthStatus"],
        "identity": ["RuntimeIdentity"],
    },
}
_LEFT = [
    (package, module, name)
    for package, modules in LEFT_THE_PACKAGES.items()
    for module, names in modules.items()
    for name in names
]

REMOVED_IN_3_5_1 = (
    ("infra2_sdk.deploy", "build_deploy_request"),
    ("infra2_sdk.deploy", "derive_release_evidence"),
    ("infra2_sdk.deploy", "main"),
    ("infra2_sdk.deploy", "dispatch_main"),
    ("infra2_sdk.deploy", "deploy_health_main"),
    ("infra2_sdk.routing", "RouteEndpoint"),
    ("infra2_sdk.routing", "AppRoutePreference"),
    ("infra2_sdk.runtime.environment", "to_deploy_type"),
    ("infra2_sdk.runtime.environment", "APP_OWNED_TIERS"),
    ("infra2_sdk.runtime.health", "health_response"),
    ("infra2_sdk.runtime.otel", "OtelSettings.from_identity"),
)


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
    assert count == len(_LEFT) == 26


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


@pytest.mark.parametrize(
    ("module", "dotted"), REMOVED_IN_3_5_1, ids=[f"{m}.{n}" for m, n in REMOVED_IN_3_5_1]
)
def test_symbols_removed_in_3_5_1_are_absent_from_modules_and_packages(
    module: str, dotted: str
) -> None:
    import infra2_sdk

    owner = importlib.import_module(module)
    *parents, leaf = dotted.split(".")
    for part in parents:
        owner = getattr(owner, part)
    assert not hasattr(owner, leaf), f"{module}.{dotted} was removed in 3.5.1"
    if not parents:
        for package in (infra2_sdk, infra2_sdk.runtime):
            assert not hasattr(package, leaf), f"{package.__name__}.{leaf} was removed in 3.5.1"
            assert leaf not in package.__all__, f"{package.__name__}.__all__ still lists {leaf}"


def test_cli_scaffold_module_removed_in_3_5_1_is_unimportable() -> None:
    import infra2_sdk

    importlib.invalidate_caches()
    for mod_name in ("infra2_sdk.cli", "infra2_sdk.cli.scaffold"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(mod_name)
    assert not hasattr(infra2_sdk, "cli")
    assert "cli" not in infra2_sdk.__all__


# --- The require_issued_identity documentation claim -------------------------------------------


def test_readme_documents_require_issued_identity_and_behavior_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import FastAPI

    from infra2_sdk.fastapi import init_service
    from infra2_sdk.runtime.otel import REQUIRED_IDENTITY_ATTRIBUTES

    parts = re.split(r"^#{2,3} (.+)$", README, flags=re.MULTILINE)
    sections = dict(zip(parts[1::2], parts[2::2], strict=True))
    for section_title in ("Runtime extras", "Turnkey FastAPI integration"):
        section_flat = " ".join(sections[section_title].split())
        assert "`require_issued_identity=True`" in section_flat
        assert "`OTEL_SERVICE_NAME`" in section_flat
        assert "`REQUIRED_IDENTITY_ATTRIBUTES`" in section_flat
        for attr in REQUIRED_IDENTITY_ATTRIBUTES:
            assert f"`{attr}`" in section_flat, (
                f"README section {section_title!r} does not name {attr}"
            )

    # 1. With an active OTLP endpoint, missing issued identity attributes fail closed.
    with pytest.raises(
        ValueError,
        match="telemetry requires ServiceIdentity attributes when an OTLP endpoint is set",
    ):
        OtelSettings.from_env(
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"},
            require_issued_identity=True,
        )

    # 2. Ignores ENVIRONMENT/ENV/APP_ENV and derives the tier from deployment.environment.name.
    issued_env = {
        "ENVIRONMENT": "not-a-valid-tier",
        "ENV": "conflicting-tier",
        "OTEL_SERVICE_NAME": "billing-api",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
        "OTEL_RESOURCE_ATTRIBUTES": (
            "deployment.environment.name=staging,"
            "infra.service.id=apps/billing-api,"
            "service.version=v1.2.3,"
            "infra.iac.ref=deadbeef"
        ),
    }
    settings = OtelSettings.from_env(issued_env, strict=True, require_issued_identity=True)
    assert settings.environment == "staging"
    assert settings.deployment_environment == "staging"
    assert settings.service_name == "billing-api"

    # 3. init_service(require_issued_identity=True) enforces the same contract before mutating app.
    for key in tuple(issued_env) + ("APP_ENV", "OTEL_SDK_DISABLED"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
    app = FastAPI()
    with pytest.raises(
        ValueError,
        match="telemetry requires ServiceIdentity attributes when an OTLP endpoint is set",
    ):
        init_service(app, name="billing-api", require_issued_identity=True)


# --- The counts that the README states ---------------------------------------------------------


def test_the_readme_counts_the_fixed_names_and_the_variables_of_the_contract() -> None:
    flat = " ".join(README.split())  # a line break in the README must not change the result
    fixed = re.search(r"(\w+) more fixed names exist outside the table\.", flat)
    assert fixed and fixed.group(1) == "Five"
    assert len(FIXED_READS) == 5
    paragraph = flat[fixed.start() : fixed.start() + 500]
    for name in FIXED_READS:
        assert f"`{name}`" in paragraph, f"the README paragraph does not name {name}"

    version_note = re.search(r"Version 2 lists (\d+) variables", flat)
    assert version_note and int(version_note.group(1)) == len(runtime_env_contract()["variables"])
    lost = re.search(r"lost the (\d+) variables above", flat)
    assert lost and int(lost.group(1)) == len(REMOVED_ENV_MEMBERS) == 20

    groups = [item for item in runtime_env_contract()["variables"] if item["aliases"]]
    assert len(groups) == 2
    assert "`init_service` reads two groups of aliases" in flat
