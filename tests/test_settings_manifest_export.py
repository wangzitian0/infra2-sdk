"""Settings manifest export: Markdown reference and the --check CLI (#239 Slice 1)."""

from __future__ import annotations

import json
import sys
import textwrap

import pytest

from infra2_sdk.runtime import environment_manifest_from_model, render_env_markdown
from infra2_sdk.runtime.config_schema import _main

pydantic = pytest.importorskip("pydantic")
pydantic_settings = pytest.importorskip("pydantic_settings")

SETTINGS = textwrap.dedent(
    """
    from pydantic import AliasChoices, Field, SecretStr
    from pydantic_settings import BaseSettings


    class Settings(BaseSettings):
        database_url: str = Field(
            description="Postgres DSN | async driver",
            json_schema_extra={"group": "Database", "source": "runtime"},
        )
        llm_api_key: SecretStr = Field(
            default=SecretStr(""),
            validation_alias=AliasChoices("LLM_API_KEY", "AI_API_KEY"),
            json_schema_extra={"group": "AI Provider", "source": "human"},
        )
        debug: bool = Field(default=False, description="Verbose logs")
    """
)


@pytest.fixture()
def settings_module(tmp_path, monkeypatch) -> str:
    (tmp_path / "fixture_settings.py").write_text(SETTINGS)
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("fixture_settings", None)
    return "fixture_settings:Settings"


def _manifest(target: str):
    from importlib import import_module

    module, _, cls = target.partition(":")
    return environment_manifest_from_model(getattr(import_module(module), cls), source="s")


def test_markdown_has_one_table_per_group_in_field_order(settings_module) -> None:
    text = render_env_markdown(_manifest(settings_module), title="Backend")
    assert text.splitlines()[0] == "# Backend"
    assert text.index("## Database") < text.index("## AI Provider") < text.index("## Application")
    assert "| `DATABASE_URL` | yes | no | runtime | Postgres DSN \\| async driver |" in text
    assert "| `LLM_API_KEY` (aliases: `AI_API_KEY`) | no | yes | human |  |" in text
    assert "| `DEBUG` | no | no | code | Verbose logs |" in text


def test_markdown_is_deterministic(settings_module) -> None:
    manifest = _manifest(settings_module)
    assert render_env_markdown(manifest) == render_env_markdown(manifest)


def test_the_cli_writes_then_check_passes_then_detects_a_stale_file(
    settings_module, tmp_path, capsys
) -> None:
    out_json, out_md = tmp_path / "required-env.json", tmp_path / "env.md"
    argv = [settings_module, "--source", "s", "--json", str(out_json), "--markdown", str(out_md)]
    assert _main(argv) == 0
    assert json.loads(out_json.read_text())["fields"][0]["env"] == "DATABASE_URL"
    assert _main([*argv, "--check"]) == 0
    out_md.write_text(out_md.read_text().replace("Verbose logs", "Old text"))
    before = out_md.read_text()
    assert _main([*argv, "--check"]) == 1
    assert out_md.read_text() == before, "--check writes nothing"
    assert "stale" in capsys.readouterr().out
    out_json.unlink()
    assert _main([*argv, "--check"]) == 1, "a missing file is stale"


def test_the_cli_needs_an_output_and_a_class_target(settings_module) -> None:
    with pytest.raises(SystemExit):
        _main([settings_module])
    with pytest.raises(ValueError, match="module:Class"):
        _main(["fixture_settings", "--json", "x.json"])
