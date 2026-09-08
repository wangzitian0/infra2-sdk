import pytest
from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from infra2_sdk.runtime.config_schema import (
    JSON_SCHEMA_DIALECT,
    EnvironmentField,
    EnvironmentManifest,
    configuration_fingerprint,
    environment_manifest_from_model,
    reconcile,
    settings_json_schema,
    validate_environment,
)
from infra2_sdk.runtime.environ import EnvironmentConflictError


class Settings(BaseSettings):
    database_url: str = Field(description="Postgres DSN", json_schema_extra={"group": "DB"})
    api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("API_KEY", "LEGACY_KEY"),
        json_schema_extra={"vault": True},
    )


class SettingsWithMalformedMetadata(BaseSettings):
    value: str = Field(default="", json_schema_extra={"extra_keys": None})


class PrefixedSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="APP_")

    database_url: str
    token: str = Field(validation_alias="TOKEN")


def test_pydantic_model_renders_open_schema_and_manifest() -> None:
    schema = settings_json_schema(Settings, title="Example settings")
    assert schema["$schema"] == JSON_SCHEMA_DIALECT
    assert schema["title"] == "Example settings"

    manifest = environment_manifest_from_model(Settings, source="example.Settings")
    assert manifest.source == "example.Settings"
    assert manifest.fields[0].env == "DATABASE_URL"
    assert manifest.fields[0].required is True
    assert manifest.fields[1].env == "API_KEY"
    assert manifest.fields[1].aliases == ("LEGACY_KEY",)
    assert manifest.fields[1].injected is True
    assert manifest.fields[1].has_default is True
    assert manifest.fields[1].sensitive is True
    assert manifest.to_json_schema()["required"] == ["DATABASE_URL"]
    assert manifest.to_dict()["contract_version"] == 2
    assert EnvironmentManifest.from_dict(manifest.to_dict()) == manifest

    prefixed = environment_manifest_from_model(PrefixedSettings)
    assert prefixed.fields[0].env == "APP_DATABASE_URL"
    assert prefixed.fields[1].env == "TOKEN"

    neutral = environment_manifest_from_model(Settings, legacy_vault_metadata=False)
    assert neutral.fields[1].injected is False
    assert neutral.fields[1].sensitive is True


def test_environment_validation_resolves_aliases_without_values() -> None:
    manifest = environment_manifest_from_model(Settings)
    result = validate_environment(manifest, {"LEGACY_KEY": "secret"})
    assert result.valid is False
    assert result.missing == ("DATABASE_URL",)
    assert result.resolved == {"API_KEY": "LEGACY_KEY"}
    assert validate_environment(manifest, {"DATABASE_URL": "postgresql://db"}).valid
    protected = validate_environment(
        manifest,
        {"DATABASE_URL": "postgresql://db"},
        require_injected=True,
    )
    assert protected.missing == ("API_KEY",)


def test_environment_validation_rejects_conflicting_alias_values_without_leaking_them() -> None:
    manifest = environment_manifest_from_model(Settings)
    equal = validate_environment(manifest, {"API_KEY": "same", "LEGACY_KEY": "same"})
    assert equal.resolved == {"API_KEY": "API_KEY"}
    secret = "do-not-leak"
    with pytest.raises(EnvironmentConflictError) as captured:
        validate_environment(manifest, {"API_KEY": secret, "LEGACY_KEY": "different"})
    assert secret not in str(captured.value)
    assert "API_KEY" in str(captured.value)
    assert "LEGACY_KEY" in str(captured.value)


def test_manifest_validation_rejects_ambiguous_contracts() -> None:
    with pytest.raises(ValueError, match="duplicate environment alias"):
        EnvironmentField("token", "TOKEN", aliases=("TOKEN",))
    with pytest.raises(ValueError, match="environment variable"):
        EnvironmentManifest(
            source="bad",
            fields=(EnvironmentField("a", "KEY"), EnvironmentField("b", "KEY")),
        )
    with pytest.raises(ValueError, match="shared"):
        EnvironmentManifest(
            source="bad",
            fields=(
                EnvironmentField("a", "FIRST", aliases=("SHARED",)),
                EnvironmentField("b", "SHARED"),
            ),
        )
    with pytest.raises(ValueError, match="unsupported"):
        EnvironmentManifest(source="bad", fields=(), contract_version=3)
    with pytest.raises(ValueError, match="integer"):
        EnvironmentManifest(source="bad", fields=(), contract_version=True)
    raw = EnvironmentManifest(source="ok", fields=()).to_dict()
    raw["contract_version"] = "bad"
    with pytest.raises(ValueError, match="contract_version"):
        EnvironmentManifest.from_dict(raw)
    raw["contract_version"] = True
    with pytest.raises(ValueError, match="integer"):
        EnvironmentManifest.from_dict(raw)


def test_non_pydantic_models_are_rejected() -> None:
    with pytest.raises(TypeError, match="model_json_schema"):
        settings_json_schema(object)
    with pytest.raises(TypeError, match="model_fields"):
        environment_manifest_from_model(object)
    manifest = environment_manifest_from_model(SettingsWithMalformedMetadata)
    assert manifest.fields[0].aliases == ()


# ----------------------------------------------------------------------------- contract v2


class SupplyChainSettings(BaseSettings):
    app_env: str = Field(default="dev", json_schema_extra={"source": "code", "injected": True})
    otel_service_name: str = Field(
        default="svc", json_schema_extra={"source": "code", "vault": True}
    )
    twelve_data_api_key: SecretStr = Field(
        default=SecretStr(""),
        json_schema_extra={"source": "human", "scope": "project", "empty_ok": True},
    )
    secret_key: SecretStr = Field(json_schema_extra={"source": "runtime"})
    image_digest: str = Field(json_schema_extra={"source": "release"})
    approved_by: str = Field(default="", json_schema_extra={"source": "decision", "empty_ok": True})
    database_url: str = Field(
        default="",
        json_schema_extra={
            "source": "runtime",
            "provided_by": "truealpha/postgres:POSTGRES_PASSWORD",
            "composed_from": "postgresql://postgres:{POSTGRES_PASSWORD}@db/app",
        },
    )
    budget: int = Field(default=20000)


def test_manifest_from_model_reads_source_classes() -> None:
    manifest = environment_manifest_from_model(SupplyChainSettings, source="sc")
    by_env = {field.env: field for field in manifest.fields}
    td = by_env["TWELVE_DATA_API_KEY"]
    assert (td.source, td.scope, td.empty_ok, td.sensitive, td.store_backed) == (
        "human",
        "project",
        True,
        True,
        True,
    )
    assert by_env["SECRET_KEY"].source == "runtime" and by_env["SECRET_KEY"].required
    assert by_env["IMAGE_DIGEST"].injected and by_env["IMAGE_DIGEST"].source == "release"
    assert by_env["APPROVED_BY"].injected and by_env["APPROVED_BY"].empty_ok
    db = by_env["DATABASE_URL"]
    assert db.provided_by == "truealpha/postgres:POSTGRES_PASSWORD"
    assert db.composed_keys == ("POSTGRES_PASSWORD",) and not db.store_backed
    assert by_env["BUDGET"].source == "code"
    assert by_env["APP_ENV"].injected and not by_env["APP_ENV"].sensitive
    legacy = by_env["OTEL_SERVICE_NAME"]
    assert legacy.injected and not legacy.sensitive and legacy.source == "code"
    assert [f.env for f in manifest.store_backed] == ["TWELVE_DATA_API_KEY", "SECRET_KEY"]
    assert [f.env for f in manifest.by_source("release", "decision")] == [
        "IMAGE_DIGEST",
        "APPROVED_BY",
    ]
    schema = manifest.to_json_schema()["properties"]["DATABASE_URL"]
    assert schema["x-source"] == "runtime" and schema["x-provided-by"] == db.provided_by
    round_trip = EnvironmentManifest.from_dict(manifest.to_dict())
    assert round_trip == manifest


def test_field_supply_chain_validation() -> None:
    with pytest.raises(ValueError, match="unknown source"):
        EnvironmentField("a", "A", source="vault")
    with pytest.raises(ValueError, match="only runtime fields can mirror"):
        EnvironmentField("a", "A", source="human", mirror_to_1password=True)
    with pytest.raises(ValueError, match="cannot be empty_ok"):
        EnvironmentField("a", "A", required=True, empty_ok=True)
    with pytest.raises(ValueError, match="project/service:KEY"):
        EnvironmentField("a", "A", provided_by="postgres")
    with pytest.raises(ValueError, match="placeholder"):
        EnvironmentField("a", "A", composed_from="static")
    composed = EnvironmentField("a", "A", composed_from="http://h:{env:PORT}/{KEY}")
    assert (composed.composed_keys, composed.composed_env, composed.rendered) == (
        ("KEY",),
        ("PORT",),
        True,
    )
    with pytest.raises(ValueError, match="scope"):
        EnvironmentField("a", "A", scope="global")
    with pytest.raises(ValueError, match="unsupported environment manifest version 1"):
        EnvironmentManifest.from_dict({"contract_version": 1, "source": "old", "fields": []})


def test_reconcile_reports_names_only() -> None:
    manifest = EnvironmentManifest(
        source="sc",
        fields=(
            EnvironmentField("a", "A", source="human", required=True, has_default=False),
            EnvironmentField("b", "B", source="runtime", empty_ok=True),
            EnvironmentField("c", "C", source="runtime", required=True, has_default=False),
            EnvironmentField("d", "D", source="code"),
            EnvironmentField("e", "E", source="release", injected=True),
        ),
    )
    report = reconcile(
        manifest,
        {"A": "old", "C": " ", "D": "x", "E": "sha", "ZOMBIE": "1", "_probe": ""},
        expected={"A": "new"},
    )
    assert report.to_dict() == {
        "missing": [],
        "empty": ["C"],
        "unclassified": ["ZOMBIE"],
        "stale": ["A"],
    }
    assert not report.ok
    assert reconcile(manifest, {"A": "v", "C": "v"}).ok
    assert reconcile(manifest, {"C": "v"}).missing == ("A",)


def test_configuration_fingerprint_is_stable_and_value_blind() -> None:
    manifest = EnvironmentManifest(
        source="sc", fields=(EnvironmentField("a", "A"), EnvironmentField("b", "B"))
    )
    one = configuration_fingerprint(manifest, {"B": "2", "A": "1", "IGNORED": "x"})
    assert one == configuration_fingerprint(manifest, {"A": "1", "B": "2"})
    assert one != configuration_fingerprint(manifest, {"A": "1", "B": "3"})
    assert len(one) == 64 and "1" not in one[:0]


def test_manifest_from_model_folds_a_side_table_of_overrides() -> None:
    from pydantic import Field
    from pydantic_settings import BaseSettings

    class Settings(BaseSettings):
        database_url: str = Field(default="postgresql://x")
        git_commit_sha: str = Field(default="unknown")
        plain: str = Field(default="p")

    manifest = environment_manifest_from_model(
        Settings,
        source="t",
        overrides={
            "database_url": {
                "source": "runtime",
                "sensitive": True,
                "provided_by": "p/postgres:PASSWORD",
            },
            "git_commit_sha": {"source": "release"},
        },
    )
    by_env = {field.env: field for field in manifest.fields}
    assert by_env["DATABASE_URL"].source == "runtime" and by_env["DATABASE_URL"].sensitive
    assert by_env["DATABASE_URL"].provided_by == "p/postgres:PASSWORD"
    assert by_env["GIT_COMMIT_SHA"].source == "release" and by_env["GIT_COMMIT_SHA"].injected
    assert by_env["PLAIN"].source == "code" and not by_env["PLAIN"].injected
    with pytest.raises(ValueError, match="unknown settings fields"):
        environment_manifest_from_model(Settings, overrides={"nope": {"source": "human"}})
