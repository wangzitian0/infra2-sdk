import pytest
from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from infra2_sdk.runtime.config_schema import (
    JSON_SCHEMA_DIALECT,
    EnvironmentField,
    EnvironmentManifest,
    environment_manifest_from_model,
    settings_json_schema,
    validate_environment,
)


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
    assert manifest.to_dict()["contract_version"] == 1
    assert EnvironmentManifest.from_dict(manifest.to_dict()) == manifest

    prefixed = environment_manifest_from_model(PrefixedSettings)
    assert prefixed.fields[0].env == "APP_DATABASE_URL"
    assert prefixed.fields[1].env == "TOKEN"


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
        EnvironmentManifest(source="bad", fields=(), contract_version=2)
    raw = EnvironmentManifest(source="ok", fields=()).to_dict()
    raw["contract_version"] = "bad"
    with pytest.raises(ValueError, match="contract_version"):
        EnvironmentManifest.from_dict(raw)


def test_non_pydantic_models_are_rejected() -> None:
    with pytest.raises(TypeError, match="model_json_schema"):
        settings_json_schema(object)
    with pytest.raises(TypeError, match="model_fields"):
        environment_manifest_from_model(object)
    manifest = environment_manifest_from_model(SettingsWithMalformedMetadata)
    assert manifest.fields[0].aliases == ()
