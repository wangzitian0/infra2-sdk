"""Tests for infra2_sdk.config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel, Field, SecretStr

from infra2_sdk import config
from infra2_sdk._core import canonical_json_bytes, canonical_sha256, parse_iso_utc


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


def test_tier_and_environment_resolution() -> None:
    assert config.resolve_tier("prod") is config.EnvironmentTier.PRODUCTION
    assert config.resolve_tier("production") is config.EnvironmentTier.PRODUCTION
    assert config.resolve_tier("staging") is config.EnvironmentTier.STAGING
    assert config.resolve_tier("local") is config.EnvironmentTier.LOCAL_DEV
    assert config.resolve_tier("test", github_actions=False) is config.EnvironmentTier.LOCAL_TEST
    assert config.resolve_tier("test", github_actions=True) is config.EnvironmentTier.GITHUB_CI
    assert config.resolve_tier("pr-42") is config.EnvironmentTier.PREVIEW
    assert config.resolve_tier("canary") is config.EnvironmentTier.PREVIEW
    assert config.resolve_tier(config.EnvironmentTier.STAGING) is config.EnvironmentTier.STAGING
    assert config.is_preview_alias("canary_preview") is True
    assert config.is_preview_alias("tag_v1_2_3") is True
    with pytest.raises(ValueError, match="unknown environment"):
        config.resolve_tier("mars")
    with pytest.raises(TypeError):
        config.resolve_tier(123)  # type: ignore[arg-type]

    env_rt = config.resolve_environment({"ENVIRONMENT": "pr_7"})
    assert env_rt.tier is config.EnvironmentTier.PREVIEW
    assert env_rt.name == "pr-7"
    assert env_rt.tier in config.PLATFORM_OWNED_TIERS

    prod_rt = config.resolve_environment({"ENVIRONMENT": "prod"})
    assert prod_rt.tier is config.EnvironmentTier.PRODUCTION
    val = config.read_runtime_env(config.RuntimeEnvKey.ENVIRONMENT, {"ENVIRONMENT": " staging "})
    assert val == "staging"
    with pytest.raises(ValueError, match="ENVIRONMENT is required"):
        config.read_runtime_env(config.RuntimeEnvKey.ENVIRONMENT, {}, required=True)
    with pytest.raises(ValueError, match="ENVIRONMENT is required"):
        config.resolve_environment({}, required=True)


def test_shared_env_contract_and_checks() -> None:
    contract = config.shared_env_contract()
    assert contract["schema_version"] == 2
    assert "retired" not in contract
    names = {e["name"] for entries in contract["groups"].values() for e in entries}
    assert {"ENVIRONMENT", "DATABASE_URL", "REDIS_URL", "S3_BUCKET", "LLM_API_KEY"} <= names
    assert config.check_shared_env(["DATABASE_URL", "OTEL_SERVICE_NAME", "CUSTOM_APP_VAR"]) == []
    violations = config.check_shared_env(["APP_ENV", "ENV", "IMAGE_TAG"])
    assert len(violations) == 3


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
    assert len(manifest.by_source(config.FieldSource.RUNTIME)) == 1
    assert "POSTGRES" in manifest.render_markdown().upper()
    fp1 = manifest.fingerprint({"PORT": "8000", "DATABASE_URL": "postgresql://u:p@h/db"})
    fp2 = manifest.fingerprint({"DATABASE_URL": "postgresql://u:p@h/db", "PORT": "8000"})
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
                composed_from="{MISSING_DEP}",
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
    assert len(errs) >= 4

    with pytest.raises(ValueError):
        config.EnvironmentField(field="a", env="")
    with pytest.raises(ValueError):
        config.EnvironmentField(field="a", env="A", scope="bad")
    with pytest.raises(ValueError):
        config.EnvironmentField(field="a", env="A", provided_by="badformat")

    json_path = tmp_path / "env.manifest.json"
    md_path = tmp_path / "env-ref.md"
    rc = config.main(
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
    assert rc == 0
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
    md_path.write_text("stale\n", encoding="utf-8")
    assert (
        config.main(
            [
                "check",
                "tests.test_config:_SampleAppSettings",
                "--source",
                "sample/app",
                "--markdown",
                str(md_path),
            ]
        )
        == 1
    )

    loaded = config.EnvironmentManifest.from_file(json_path)
    diff_m = config.EnvironmentManifest.from_model(_SampleAppSettings, source="other")
    merged = config.EnvironmentManifest.merge([("a", loaded), ("b", diff_m)], source="merged")
    assert len(merged.fields) == len(loaded.fields)
    with pytest.raises(ValueError, match="declared differently"):
        config.EnvironmentManifest.merge([("a", manifest), ("b", diff_m)], source="conflict")

    aliased = config.EnvironmentField(
        field="db", env="DB_URL", aliases=("OLD_DB_URL",), required=True
    )
    alias_m = config.EnvironmentManifest(source="alias", fields=(aliased,))
    assert config.validate_env(alias_m, environ={"DB_URL": "a", "OLD_DB_URL": "b"}).conflicts == (
        "DB_URL (DB_URL, OLD_DB_URL)",
    )

    res_bad = config.validate_env(
        loaded, environ={"PORT": "abc", "DATABASE_URL": ""}, require_injected=True
    )
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

    assert len(canonical_sha256({"a": 1, "b": 2})) == 64
    assert canonical_json_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert parse_iso_utc("2026-11-25T10:00:00Z").tzinfo is not None
    with pytest.raises(ValueError, match="time zone"):
        parse_iso_utc("2026-11-25T10:00:00")
    assert json.loads(json_path.read_text(encoding="utf-8"))["source"] == "sample/app"


def test_validate_env_allows_empty_string_for_empty_ok_injected_field() -> None:
    manifest = config.EnvironmentManifest(
        source="demo/empty-ok",
        fields=(
            config.EnvironmentField(
                field="opt_endpoint",
                env="OPT_ENDPOINT",
                injected=True,
                empty_ok=True,
            ),
            config.EnvironmentField(
                field="req_dsn",
                env="REQ_DSN",
                injected=True,
                empty_ok=False,
            ),
        ),
    )

    ok_env = {"OPT_ENDPOINT": "", "REQ_DSN": "postgresql://u:p@h/db"}
    res_ok = config.validate_env(manifest, ok_env, require_injected=True)
    assert res_ok.ok is True
    assert res_ok.missing == ()
    assert res_ok.conflicts == ()
    config.assert_env(manifest, environ=ok_env, require_injected=True)

    res_absent = config.validate_env(
        manifest, {"REQ_DSN": "postgresql://u:p@h/db"}, require_injected=True
    )
    assert res_absent.ok is False and res_absent.missing == ("OPT_ENDPOINT",)

    res_non_empty_rejected = config.validate_env(
        manifest, {"OPT_ENDPOINT": "", "REQ_DSN": ""}, require_injected=True
    )
    assert res_non_empty_rejected.ok is False and res_non_empty_rejected.missing == ("REQ_DSN",)
