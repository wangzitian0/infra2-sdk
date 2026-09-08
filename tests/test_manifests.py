"""infra2_sdk.manifests: the one generate/check/validate driver for application repositories."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import Field
from pydantic_settings import BaseSettings

from infra2_sdk import manifests
from infra2_sdk.manifests import ManifestSpec


class Settings(BaseSettings):
    database_url: str = Field(
        default="postgresql://x",
        json_schema_extra={
            "source": "runtime",
            "sensitive": True,
            "provided_by": "p/postgres:PASSWORD",
        },
    )
    api_key: str = Field(
        default="", json_schema_extra={"source": "human", "sensitive": True, "empty_ok": True}
    )
    git_commit_sha: str = Field(default="unknown", json_schema_extra={"source": "release"})
    page_size: int = Field(default=20)


SIDE_TABLE = {"page_size": {"group": "Paging"}}
SPEC = ManifestSpec(
    "apps/x/required-env.generated.json", Settings, source="apps/x", overrides=SIDE_TABLE
)


def test_write_then_check_is_clean_and_edits_are_stale(tmp_path: Path) -> None:
    echoed: list[str] = []
    assert manifests.main(["--write"], root=tmp_path, specs=(SPEC,), echo=echoed.append) == 0
    written = json.loads((tmp_path / SPEC.path).read_text(encoding="utf-8"))
    assert written["contract_version"] == 2 and written["source"] == "apps/x"
    by_env = {f["env"]: f for f in written["fields"]}
    assert by_env["PAGE_SIZE"]["group"] == "Paging"  # the side table reached the file
    assert by_env["GIT_COMMIT_SHA"]["injected"] is True
    assert manifests.check(root=tmp_path, specs=(SPEC,)) == []
    (tmp_path / SPEC.path).write_text("{}\n", encoding="utf-8")
    problems = manifests.check(root=tmp_path, specs=(SPEC,))
    assert problems == [f"{SPEC.path}: stale, regenerate with --write"]
    assert manifests.main(["--check"], root=tmp_path, specs=(SPEC,), echo=echoed.append) == 1
    assert echoed[-1] == "env manifests: 1 problem(s)"


def test_hand_written_manifests_go_through_the_offline_gate(tmp_path: Path) -> None:
    bad = {
        "contract_version": 2,
        "source": "web",
        "fields": [
            {
                "field": "secret",
                "env": "SECRET",
                "source": "code",
                "sensitive": True,
                "required": True,
            }
        ],
    }
    (tmp_path / "web.json").write_text(json.dumps(bad), encoding="utf-8")
    problems = manifests.check(root=tmp_path, specs=(), hand_written=("web.json",))
    assert problems and problems[0].startswith("web.json: ")


def test_validate_env_names_only_what_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifests.write(root=tmp_path, specs=(SPEC,), echo=lambda _: None)
    environ = {"DATABASE_URL": "postgresql://real", "API_KEY": ""}
    missing = manifests.validate_env(SPEC.path, environ, root=tmp_path)
    assert missing == ["GIT_COMMIT_SHA"]  # injected by the deployment, absent here
    assert manifests.validate_env(SPEC.path, environ, root=tmp_path, require_injected=False) == []
    echoed: list[str] = []
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("GIT_COMMIT_SHA", raising=False)
    assert (
        manifests.main(
            ["--validate-env", SPEC.path], root=tmp_path, specs=(SPEC,), echo=echoed.append
        )
        == 1
    )
    assert echoed == ["missing: GIT_COMMIT_SHA", "environment: 1 missing"]
    assert (
        manifests.main(
            ["--validate-env", SPEC.path, "--no-require-injected"],
            root=tmp_path,
            specs=(SPEC,),
            echo=echoed.append,
        )
        == 0
    )


def test_specs_resolve_dotted_models_and_side_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    import types

    module = types.ModuleType("spec_models_under_test")
    module.Settings = Settings
    module.SIDE_TABLE = SIDE_TABLE
    monkeypatch.setitem(sys.modules, "spec_models_under_test", module)
    spec = ManifestSpec(
        "x.json", "spec_models_under_test:Settings", overrides="spec_models_under_test:SIDE_TABLE"
    )
    manifest = spec.build()
    assert {f.env for f in manifest.fields} == {
        "DATABASE_URL",
        "API_KEY",
        "GIT_COMMIT_SHA",
        "PAGE_SIZE",
    }
    with pytest.raises(ValueError, match="package.module:Class"):
        ManifestSpec("x.json", "spec_models_under_test").resolve_model()
