"""Tests for infra2_sdk.config."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field, SecretStr

from infra2_sdk import _core, config


class _SampleAppSettings(BaseModel):
    port: int = Field(default=8000, description="HTTP listen port")
    database_url: str = Field(
        description="PostgreSQL DSN",
        json_schema_extra={"source": "runtime", "sensitive": True, "group": "postgres"},
    )
    api_token: SecretStr = Field(
        description="Service API token",
        json_schema_extra={"source": "human", "group": "auth"},
    )


class _BadSensitivitySettings(BaseModel):
    database_url: str = Field(
        description="PostgreSQL DSN wrongly marked non-sensitive",
        json_schema_extra={"source": "runtime", "sensitive": False, "group": "postgres"},
    )


def test_tier_and_environment_resolution() -> None:
    assert config.resolve_tier("prod") is config.EnvironmentTier.PRODUCTION
    assert config.resolve_tier("production") is config.EnvironmentTier.PRODUCTION
    assert config.resolve_tier("staging") is config.EnvironmentTier.STAGING
    assert config.resolve_tier("local") is config.EnvironmentTier.LOCAL_DEV
    assert config.resolve_tier("test", github_actions=False) is config.EnvironmentTier.LOCAL_TEST
    assert config.resolve_tier("test", github_actions=True) is config.EnvironmentTier.GITHUB_CI
    assert config.resolve_tier("pr-42") is config.EnvironmentTier.PREVIEW
    assert config.resolve_tier("canary") is config.EnvironmentTier.PREVIEW
    assert _core.is_preview_alias("canary") is True
    assert _core.is_preview_alias("canary_preview") is True
    assert _core.is_preview_alias("preview/pr-7") is True
    assert _core.is_preview_alias("tag_v1_2_3") is True
    assert _core.is_preview_alias("staging") is False
    with pytest.raises(ValueError, match="unknown environment"):
        config.resolve_tier("mars")
    with pytest.raises(TypeError):
        config.resolve_tier(123)  # type: ignore[arg-type]

    env_rt = _core.resolve_environment({"ENVIRONMENT": "pr_7"})
    assert env_rt.tier is config.EnvironmentTier.PREVIEW
    assert env_rt.name == "pr-7"
    assert env_rt.tier in _core.platform_owned_tiers()

    assert (
        _core.read_runtime_env(config.RuntimeEnvKey.ENVIRONMENT, {"ENVIRONMENT": " staging "})
        == "staging"
    )
    with pytest.raises(ValueError, match="ENVIRONMENT is required"):
        _core.read_runtime_env(config.RuntimeEnvKey.ENVIRONMENT, {}, required=True)
    with pytest.raises(ValueError, match="ENVIRONMENT is required"):
        _core.resolve_environment({}, required=True)


def test_manifest_lifecycle_offline_checks_and_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest = config.EnvironmentManifest.from_model(
        _SampleAppSettings,
        source="sample/app",
        overrides={"port": {"group": "server", "source": "release"}},
    )
    assert manifest.validate_offline() == []
    assert [f.env for f in manifest.store_backed()] == ["DATABASE_URL", "API_TOKEN"]
    fp1 = manifest.fingerprint({"PORT": "8000", "DATABASE_URL": "postgresql://u:p@h/db"})
    fp2 = manifest.fingerprint(
        {"DATABASE_URL": "postgresql://u:p@h/db", "PORT": "8000", "API_TOKEN": ""}
    )
    assert fp1 == fp2 and len(fp1) == 64

    bad_sens = config.EnvironmentManifest.from_model(_BadSensitivitySettings, source="bad-sens")
    assert any("sensitive=True" in e for e in bad_sens.validate_offline())

    bad_manifest = config.EnvironmentManifest(
        source="bad",
        fields=(
            config.EnvironmentField(
                field="x",
                env="X_VAR",
                source="runtime",
                has_default=True,
                required=False,
                injected=False,
            ),
            config.EnvironmentField(
                field="y",
                env="Y_VAR",
                source="code",
                sensitive=True,
            ),
            config.EnvironmentField(
                field="z",
                env="Z_VAR",
                source="bootstrap",
                injected=True,
                composed_from="{MISSING_DEP}/{env:bad}",
            ),
            config.EnvironmentField(
                field="w",
                env="W_VAR",
                source="release",
                injected=False,
            ),
        ),
    )
    errs = bad_manifest.validate_offline()
    assert len(errs) >= 5 and any("composed_from env:bad" in e for e in errs)

    # Retired environment names are rejected by validate_offline().
    for retired in ("APP_ENV", "IMAGE_TAG", "SERVICE_NAME"):
        retired_m = config.EnvironmentManifest(
            source="retired",
            fields=(config.EnvironmentField(field=retired.lower(), env=retired),),
        )
        assert any("retired environment variable name" in e for e in retired_m.validate_offline())

    json_path = tmp_path / "env.manifest.json"
    assert (
        config._main(
            [
                "export",
                "tests.test_config:_SampleAppSettings",
                "--source",
                "sample/app",
                "--json",
                str(json_path),
            ]
        )
        == 0
    )
    assert (
        config._main(
            [
                "check",
                "tests.test_config:_SampleAppSettings",
                "--source",
                "sample/app",
                "--json",
                str(json_path),
            ]
        )
        == 0
    )
    json_path.write_text("{}\n", encoding="utf-8")
    assert (
        config._main(
            [
                "check",
                "tests.test_config:_SampleAppSettings",
                "--source",
                "sample/app",
                "--json",
                str(json_path),
            ]
        )
        == 1
    )
    assert (
        config._main(
            [
                "export",
                "tests.test_config:_SampleAppSettings",
                "--source",
                "sample/app",
                "--json",
                str(json_path),
            ]
        )
        == 0
    )

    loaded = config.EnvironmentManifest.from_file(json_path)
    diff_m = config.EnvironmentManifest.from_model(_SampleAppSettings, source="other")
    merged = config.EnvironmentManifest.merge([("a", loaded), ("b", diff_m)], source="merged")
    assert len(merged.fields) == len(loaded.fields)
    with pytest.raises(ValueError, match="declared differently"):
        config.EnvironmentManifest.merge([("a", manifest), ("b", diff_m)], source="conflict")

    sens_a = config.EnvironmentManifest(
        source="s1",
        fields=(config.EnvironmentField(field="k", env="SHARED_KEY", sensitive=True),),
    )
    sens_b = config.EnvironmentManifest(
        source="s2",
        fields=(config.EnvironmentField(field="k", env="SHARED_KEY", sensitive=False),),
    )
    with pytest.raises(ValueError, match="SHARED_KEY.sensitive"):
        config.EnvironmentManifest.merge([("s1", sens_a), ("s2", sens_b)], source="sens-conflict")

    good_env = {"DATABASE_URL": "postgresql://u:p@h/db", "API_TOKEN": "tok-1234", "PORT": "8080"}
    config.assert_env(json_path, environ=good_env, require_injected=True)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        config.assert_env(loaded, environ={"DATABASE_URL": ""}, require_injected=True)

    capsys.readouterr()
    assert config._main(["validate", str(json_path)]) == 1
    assert "error:" in capsys.readouterr().err
    for k, v in good_env.items():
        monkeypatch.setenv(k, v)
    assert config._main(["validate", str(json_path)]) == 0

    digest_ab = _core.canonical_sha256({"a": 1, "b": 2})
    assert len(digest_ab) == 64
    assert _core.canonical_sha256({"b": 2, "a": 1}) == digest_ab
    assert _core.parse_iso_utc("2026-11-25T10:00:00Z").tzinfo is not None
    with pytest.raises(ValueError, match="time zone"):
        _core.parse_iso_utc("2026-11-25T10:00:00")


def test_assert_env_allows_empty_string_for_empty_ok_injected_field() -> None:
    manifest = config.EnvironmentManifest(
        source="demo/empty-ok",
        fields=(
            config.EnvironmentField(
                field="opt_endpoint", env="OPT_ENDPOINT", injected=True, empty_ok=True
            ),
            config.EnvironmentField(field="req_dsn", env="REQ_DSN", injected=True, empty_ok=False),
        ),
    )

    ok_env = {"OPT_ENDPOINT": "", "REQ_DSN": "postgresql://u:p@h/db"}
    config.assert_env(manifest, ok_env, require_injected=True)
    with pytest.raises(RuntimeError, match="OPT_ENDPOINT"):
        config.assert_env(manifest, {"REQ_DSN": "postgresql://u:p@h/db"}, require_injected=True)
    with pytest.raises(RuntimeError, match="REQ_DSN"):
        config.assert_env(manifest, {"OPT_ENDPOINT": "", "REQ_DSN": ""}, require_injected=True)


def test_validate_offline_allows_sensitive_code_field_with_composed_from() -> None:
    pw = config.EnvironmentField(
        field="app_pw",
        env="APP_SERVICE_DB_PASSWORD",
        source="runtime",
        sensitive=True,
        empty_ok=True,
    )
    composed_dsn = config.EnvironmentField(
        field="database_url",
        env="DATABASE_URL",
        source="code",
        sensitive=True,
        composed_from="postgresql://app:{APP_SERVICE_DB_PASSWORD}@db:5432/app",
    )
    bad_default = config.EnvironmentField(
        field="api_key",
        env="API_KEY",
        source="code",
        sensitive=True,
    )
    assert (
        config.EnvironmentManifest(source="app", fields=(pw, composed_dsn)).validate_offline() == []
    )
    errs = config.EnvironmentManifest(source="app", fields=(bad_default,)).validate_offline()
    assert errs == ["API_KEY: a sensitive value cannot be a code default"]


def test_environment_manifest_v2_wire_format_preserves_aliases() -> None:
    raw = {
        "contract_version": 2,
        "source": "app/service",
        "fields": [
            {"field": "port", "env": "PORT", "aliases": []},
            {"field": "api_key", "env": "API_KEY", "aliases": ["OLD_KEY"], "source": "runtime"},
        ],
    }
    manifest = config.EnvironmentManifest.from_dict(raw)
    assert [f.aliases for f in manifest.fields] == [(), ("OLD_KEY",)]
    dumped = manifest.to_dict()
    assert dumped["fields"][0]["aliases"] == []
    assert dumped["fields"][1]["aliases"] == ["OLD_KEY"]

    with pytest.raises(ValueError, match="duplicate environment alias"):
        config.EnvironmentField(field="bad", env="FOO", aliases=("FOO",))

    with pytest.raises(ValueError, match="shared by"):
        config.EnvironmentManifest.from_dict(
            {
                "contract_version": 2,
                "source": "app/conflict",
                "fields": [
                    {"field": "a", "env": "NEW_KEY", "aliases": ["SHARED_KEY"]},
                    {"field": "b", "env": "SHARED_KEY", "aliases": []},
                ],
            }
        )
