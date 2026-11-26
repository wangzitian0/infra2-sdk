"""Tests for infra2_sdk.config."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field, SecretStr

from infra2_sdk import config
from infra2_sdk._core import canonical_sha256, parse_iso_utc


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
    assert config.is_preview_alias("canary") is True
    assert config.is_preview_alias("canary_preview") is True
    assert config.is_preview_alias("preview/pr-7") is True
    assert config.is_preview_alias("tag_v1_2_3") is True
    assert config.is_preview_alias("staging") is False
    with pytest.raises(ValueError, match="unknown environment"):
        config.resolve_tier("mars")
    with pytest.raises(TypeError):
        config.resolve_tier(123)  # type: ignore[arg-type]

    env_rt = config.resolve_environment({"ENVIRONMENT": "pr_7"})
    assert env_rt.tier is config.EnvironmentTier.PREVIEW
    assert env_rt.name == "pr-7"
    assert env_rt.tier in config.PLATFORM_OWNED_TIERS

    assert (
        config.read_runtime_env(config.RuntimeEnvKey.ENVIRONMENT, {"ENVIRONMENT": " staging "})
        == "staging"
    )
    with pytest.raises(ValueError, match="ENVIRONMENT is required"):
        config.read_runtime_env(config.RuntimeEnvKey.ENVIRONMENT, {}, required=True)
    with pytest.raises(ValueError, match="ENVIRONMENT is required"):
        config.resolve_environment({}, required=True)


def test_shared_env_contract_and_checks() -> None:
    contract = config.shared_env_contract()
    assert contract["schema_version"] == 2
    contract["groups"].clear()
    assert config.shared_env_contract()["groups"]  # caller mutation never corrupts cache

    assert config.check_shared_env(["DATABASE_URL", "OTEL_SERVICE_NAME", "CUSTOM_APP_VAR"]) == []
    violations = config.check_shared_env(["database_url", "bad-name"])
    assert any("canonical spelling DATABASE_URL" in item for item in violations)
    assert any("POSIX" in item for item in violations)

    bad_manifest = config.EnvironmentManifest.from_model(_BadSensitivitySettings, source="bad")
    sens_issues = config.check_shared_env(bad_manifest)
    assert any("sensitive=True" in item for item in sens_issues)


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
    assert "POSTGRES" in manifest.render_markdown().upper()
    fp1 = manifest.fingerprint({"PORT": "8000", "DATABASE_URL": "postgresql://u:p@h/db"})
    fp2 = manifest.fingerprint(
        {"DATABASE_URL": "postgresql://u:p@h/db", "PORT": "8000", "API_TOKEN": ""}
    )
    assert fp1 == fp2 and len(fp1) == 64

    bad_manifest = config.EnvironmentManifest(
        source="bad",
        fields=(
            config.EnvironmentField(
                field="x",
                env="X_VAR",
                source=config.FieldSource.RUNTIME,
                has_default=True,
                required=False,
                injected=False,
            ),
            config.EnvironmentField(
                field="y",
                env="Y_VAR",
                source=config.FieldSource.CODE,
                sensitive=True,
            ),
            config.EnvironmentField(
                field="z",
                env="Z_VAR",
                source=config.FieldSource.BOOTSTRAP,
                injected=True,
                composed_from="{MISSING_DEP}/{env:bad}",
            ),
            config.EnvironmentField(
                field="w",
                env="W_VAR",
                source=config.FieldSource.RELEASE,
                injected=False,
            ),
        ),
    )
    errs = bad_manifest.validate_offline()
    assert len(errs) >= 5 and any("composed_from env:bad" in e for e in errs)

    json_path = tmp_path / "env.manifest.json"
    md_path = tmp_path / "env-ref.md"
    assert (
        config.main(
            [
                "export",
                "tests.test_config:_SampleAppSettings",
                "--source",
                "sample/app",
                "--json",
                str(json_path),
                "--markdown",
                str(md_path),
            ]
        )
        == 0
    )
    assert (
        config.main(
            [
                "check",
                "tests.test_config:_SampleAppSettings",
                "--source",
                "sample/app",
                "--json",
                str(json_path),
                "--markdown",
                str(md_path),
            ]
        )
        == 0
    )
    assert config.main(["export", "tests.test_config:_SampleAppSettings"]) == 1
    md_path.write_text("stale\n", encoding="utf-8")
    assert (
        config.main(["check", "tests.test_config:_SampleAppSettings", "--markdown", str(md_path)])
        == 1
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

    aliased = config.EnvironmentField(
        field="db", env="DB_URL", aliases=("OLD_DB_URL",), required=True
    )
    alias_m = config.EnvironmentManifest(source="alias", fields=(aliased,))
    assert config.validate_env(alias_m, environ={"DB_URL": "a", "OLD_DB_URL": "b"}).conflicts == (
        "DB_URL (DB_URL, OLD_DB_URL)",
    )
    assert alias_m.fingerprint({"OLD_DB_URL": "abc"}) == alias_m.fingerprint({"DB_URL": "abc"})

    res_bad = config.validate_env(loaded, environ={"DATABASE_URL": ""}, require_injected=True)
    assert res_bad.ok is False and "DATABASE_URL" in res_bad.missing

    good_env = {"DATABASE_URL": "postgresql://u:p@h/db", "API_TOKEN": "tok-1234", "PORT": "8080"}
    config.assert_env(json_path, environ=good_env, require_injected=True)
    with pytest.raises(RuntimeError):
        config.assert_env(loaded, environ={}, require_injected=True)

    capsys.readouterr()
    assert config.main(["validate", str(json_path)]) == 1
    assert "error:" in capsys.readouterr().err
    for k, v in good_env.items():
        monkeypatch.setenv(k, v)
    assert config.main(["validate", str(json_path)]) == 0

    digest_ab = canonical_sha256({"a": 1, "b": 2})
    assert len(digest_ab) == 64
    assert canonical_sha256({"b": 2, "a": 1}) == digest_ab
    assert parse_iso_utc("2026-11-25T10:00:00Z").tzinfo is not None
    with pytest.raises(ValueError, match="time zone"):
        parse_iso_utc("2026-11-25T10:00:00")


def test_validate_env_allows_empty_string_for_empty_ok_injected_field() -> None:
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
    assert config.validate_env(manifest, ok_env, require_injected=True).ok is True
    assert config.validate_env(
        manifest, {"REQ_DSN": "postgresql://u:p@h/db"}, require_injected=True
    ).missing == ("OPT_ENDPOINT",)
    assert config.validate_env(
        manifest, {"OPT_ENDPOINT": "", "REQ_DSN": ""}, require_injected=True
    ).missing == ("REQ_DSN",)


def test_validate_offline_allows_sensitive_code_field_with_composed_from() -> None:
    pw = config.EnvironmentField(
        field="app_pw",
        env="APP_SERVICE_DB_PASSWORD",
        source=config.FieldSource.RUNTIME,
        sensitive=True,
        empty_ok=True,
    )
    composed_dsn = config.EnvironmentField(
        field="database_url",
        env="DATABASE_URL",
        source=config.FieldSource.CODE,
        sensitive=True,
        composed_from="postgresql://app:{APP_SERVICE_DB_PASSWORD}@db:5432/app",
    )
    bad_default = config.EnvironmentField(
        field="api_key",
        env="API_KEY",
        source=config.FieldSource.CODE,
        sensitive=True,
    )
    assert (
        config.EnvironmentManifest(source="app", fields=(pw, composed_dsn)).validate_offline() == []
    )
    errs = config.EnvironmentManifest(source="app", fields=(bad_default,)).validate_offline()
    assert errs == ["API_KEY: a sensitive value cannot be a code default"]
