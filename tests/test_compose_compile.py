"""Tests for `compile_compose`: baseline defaults and the `x-vault` sidecar expansion.

The fixtures in `tests/fixtures/compose/` come from `wangzitian0/infra2@4dd5ed29`
`platform/30.todo/`. Regenerate the golden output with:

    uv run python -c "from pathlib import Path; from infra2_sdk.rules import compile_compose; \
d = Path('tests/fixtures/compose'); \
(d / 'todo.compiled.compose.yaml').write_text(compile_compose( \
(d / 'todo.x-infra2.compose.yaml').read_text(), \
vault_template=(d / 'todo.secrets.ctmpl').read_text().split('\\n', 1)[1]))"

The equivalence check against the real infra2 files runs in infra2 CI (infra2#1822).
"""

from __future__ import annotations

import copy
import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from infra2_sdk.rules import ComposeCompileError, compile_compose, inspect_compose

FIXTURES = Path(__file__).with_name("fixtures") / "compose"
SINGLE_DOLLAR = re.compile(r"(?<!\$)\$(?!\$)")


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _body(name: str) -> str:
    """Return a fixture without its first provenance line."""
    return _read(name).split("\n", 1)[1]


def _compiled_todo() -> dict[str, Any]:
    text = compile_compose(
        _read("todo.x-infra2.compose.yaml"), vault_template=_body("todo.secrets.ctmpl")
    )
    return yaml.safe_load(text)


def _minimal(**service: Any) -> dict[str, Any]:
    spec = {"image": "alpine:3.20", "mem_limit": "64m", **service}
    return {"x-infra2": {}, "services": {"app": spec}}


def _vault_source() -> dict[str, Any]:
    return {
        "x-infra2": {"vault": {"name": "stack-a"}},
        "services": {
            "api": {"image": "api:1", "mem_limit": "64m", "command": ["run-api"], "x-vault": {}},
            "worker": {
                "image": "api:1",
                "mem_limit": "64m",
                "command": ["run-worker"],
                "depends_on": ["api"],
                "x-vault": {"max_wait_seconds": 30},
            },
            "web": {"image": "web:1", "mem_limit": "64m"},
        },
    }


def _strip_comments(script: str) -> list[str]:
    return [
        ln.rstrip() for ln in script.splitlines() if ln.strip() and not ln.lstrip().startswith("#")
    ]


# --- equivalence with the real 30.todo compose -------------------------------------------


def test_compiled_todo_app_equals_handwritten_after_normalization() -> None:
    hand = yaml.safe_load(_read("todo.handwritten.compose.yaml"))["services"]["todo"]
    compiled = _compiled_todo()["services"]["todo"]
    # Normalization 1: the hand entrypoint ends in `exec <command>`; the compiler uses
    # `exec "$@"` plus `command`. Assert that both run the same process, then drop both.
    hand_script = hand.pop("entrypoint")[2]
    assert _strip_comments(hand_script)[-1] == "exec " + " ".join(compiled["command"])
    del compiled["command"], compiled["entrypoint"]
    assert compiled == hand


def test_compiled_todo_sidecar_equals_handwritten_after_normalization() -> None:
    hand = yaml.safe_load(_read("todo.handwritten.compose.yaml"))["services"]["vault-agent"]
    compiled = _compiled_todo()["services"]["vault-agent"]
    # Normalization 1: the hand healthcheck script holds comment lines; compare code lines.
    assert _strip_comments(compiled["healthcheck"]["test"][1]) == _strip_comments(
        hand["healthcheck"]["test"][1]
    )
    for spec in (hand, compiled):
        del spec["healthcheck"]["test"]
    # Normalization 2: the file mounts, their hash labels and the file-writing entrypoint
    # are the three intended differences. The two new environment keys carry the files.
    for key in ("volumes", "labels", "entrypoint"):
        del hand[key], compiled[key]
    for key in ("VAULT_AGENT_HCL", "VAULT_SECRETS_TEMPLATE"):
        del compiled["environment"][key]
    assert compiled == hand


def test_compiled_todo_template_and_config_equal_handwritten_files() -> None:
    env = _compiled_todo()["services"]["vault-agent"]["environment"]
    # Compose turns `$$` into `$` when it interpolates, so undo the escape before comparing.
    assert env["VAULT_SECRETS_TEMPLATE"].replace("$$", "$") == _body("todo.secrets.ctmpl")
    hand_hcl = _strip_comments(_body("todo.vault-agent.hcl").replace("/etc/vault/", "/vault/"))
    assert _strip_comments(env["VAULT_AGENT_HCL"].replace("$$", "$")) == hand_hcl


def test_compiled_todo_top_level_blocks_equal_handwritten() -> None:
    hand = yaml.safe_load(_read("todo.handwritten.compose.yaml"))
    compiled = _compiled_todo()
    assert compiled["volumes"] == hand["volumes"]
    assert compiled["networks"] == hand["networks"]
    assert list(compiled["services"]) == ["vault-agent", "todo"]


# --- determinism --------------------------------------------------------------------------


def test_compile_output_equals_committed_golden_bytes() -> None:
    text = compile_compose(
        _read("todo.x-infra2.compose.yaml"), vault_template=_body("todo.secrets.ctmpl")
    )
    assert text == _read("todo.compiled.compose.yaml")


def test_compile_text_and_mapping_input_give_identical_bytes() -> None:
    source = _read("todo.x-infra2.compose.yaml")
    template = _body("todo.secrets.ctmpl")
    from_text = compile_compose(source, vault_template=template)
    from_mapping = compile_compose(yaml.safe_load(source), vault_template=template)
    assert from_text == from_mapping
    assert (
        hashlib.sha256(from_text.encode()).hexdigest()
        == hashlib.sha256(compile_compose(source, vault_template=template).encode()).hexdigest()
    )


def test_compile_does_not_mutate_its_input() -> None:
    source = _vault_source()
    before = copy.deepcopy(source)
    compile_compose(source, vault_template="X")
    assert source == before


# --- $ escaping ---------------------------------------------------------------------------


def test_dollar_in_template_is_escaped_for_compose_interpolation() -> None:
    template = '{{- $env := env "ENV" -}}\nA={{ printf "%q" ${X} }}\n'
    text = compile_compose(_vault_source(), vault_template=template)
    env = yaml.safe_load(text)["services"]["vault-agent"]["environment"]
    assert env["VAULT_SECRETS_TEMPLATE"] == template.replace("$", "$$")
    assert env["VAULT_SECRETS_TEMPLATE"].replace("$$", "$") == template


def test_sidecar_and_wrapper_scripts_hold_no_unescaped_dollar() -> None:
    services = yaml.safe_load(compile_compose(_vault_source(), vault_template="$x"))["services"]
    scripts = [
        services["vault-agent"]["entrypoint"][2],
        services["vault-agent"]["healthcheck"]["test"][1],
        services["vault-agent"]["environment"]["VAULT_AGENT_HCL"],
        services["vault-agent"]["environment"]["VAULT_SECRETS_TEMPLATE"],
        services["api"]["entrypoint"][2],
    ]
    for script in scripts:
        assert not SINGLE_DOLLAR.search(script), script


def test_user_written_interpolation_is_not_escaped() -> None:
    source = _vault_source()
    source["services"]["web"]["environment"] = {"A": "${ENV}", "B": "$$literal"}
    env = yaml.safe_load(compile_compose(source, vault_template="X"))["services"]["web"][
        "environment"
    ]
    assert env == {"A": "${ENV}", "B": "$$literal"}


# --- x-vault expansion --------------------------------------------------------------------


def test_sidecar_writes_config_and_template_to_files_and_reads_them() -> None:
    sidecar = yaml.safe_load(compile_compose(_vault_source(), vault_template="T"))["services"][
        "vault-agent"
    ]
    script = sidecar["entrypoint"][2]
    hcl = sidecar["environment"]["VAULT_AGENT_HCL"]
    source_path = re.search(r'source\s*=\s*"([^"]+)"', hcl)
    assert source_path is not None
    assert f"> {source_path.group(1)}" in script
    assert '"$$VAULT_AGENT_HCL" > /vault/agent.hcl' in script
    assert script.rstrip().endswith("exec vault agent -config=/vault/agent.hcl")
    assert sidecar["environment"]["VAULT_SECRETS_TEMPLATE"] == "T"


def test_compiled_stack_has_no_host_relative_mount() -> None:
    text = compile_compose(_vault_source(), vault_template="T")
    for spec in yaml.safe_load(text)["services"].values():
        for volume in spec.get("volumes", []):
            assert not volume.startswith((".", "/")), volume
        assert "build" not in spec


def test_template_hash_label_matches_template_bytes() -> None:
    labels = yaml.safe_load(compile_compose(_vault_source(), vault_template="T1"))["services"][
        "vault-agent"
    ]["labels"]
    expected = "infra2.vault.template.sha256=" + hashlib.sha256(b"T1").hexdigest()
    assert expected in labels
    other = yaml.safe_load(compile_compose(_vault_source(), vault_template="T2"))["services"][
        "vault-agent"
    ]["labels"]
    assert expected not in other


def test_wrapper_waits_for_secrets_then_execs_the_command_with_its_arguments() -> None:
    api = yaml.safe_load(compile_compose(_vault_source(), vault_template="T"))["services"]["api"]
    entrypoint = api["entrypoint"]
    assert entrypoint[:2] == ["sh", "-c"] and entrypoint[3] == "--"
    lines = _strip_comments(entrypoint[2])
    assert lines[-1] == 'exec "$$@"'
    assert lines.index(". /secrets/.env") < len(lines) - 1
    assert 'while [ ! -s /secrets/.env ] && [ "$$waited" -lt "$$max_wait" ]; do' in lines


def test_two_x_vault_services_share_one_sidecar_and_plain_service_is_untouched() -> None:
    services = yaml.safe_load(compile_compose(_vault_source(), vault_template="T"))["services"]
    assert list(services) == ["vault-agent", "api", "worker", "web"]
    assert services["api"]["depends_on"] == ["vault-agent"]
    assert services["worker"]["depends_on"] == ["api", "vault-agent"]
    assert 'max_wait="30"' in services["worker"]["entrypoint"][2]
    assert 'max_wait="300"' in services["api"]["entrypoint"][2]
    assert services["api"]["command"] == ["run-api"]
    web = services["web"]
    assert "entrypoint" not in web and "depends_on" not in web and "volumes" not in web
    assert "CONFIGURATION_SHA256" not in web.get("environment", {})


# --- defaults -----------------------------------------------------------------------------


def test_defaults_fill_missing_keys_and_keep_explicit_values() -> None:
    plain = yaml.safe_load(compile_compose(_minimal()))
    spec = plain["services"]["app"]
    assert spec["restart"] == "always"
    assert spec["logging"] == {
        "driver": "json-file",
        "options": {"max-size": "5m", "max-file": "2"},
    }
    assert spec["networks"] == ["dokploy-network"]
    assert plain["networks"] == {"dokploy-network": {"external": True}}
    assert "volumes" not in plain and "vault-agent" not in plain["services"]

    explicit = _minimal(restart="no", logging={"driver": "none"}, networks=["other"])
    spec = yaml.safe_load(compile_compose(explicit))["services"]["app"]
    assert (spec["restart"], spec["logging"], spec["networks"]) == (
        "no",
        {"driver": "none"},
        ["other"],
    )


def test_defaults_skip_networks_when_network_mode_is_set() -> None:
    spec = yaml.safe_load(compile_compose(_minimal(network_mode="host")))["services"]["app"]
    assert "networks" not in spec


# --- fail-closed errors -------------------------------------------------------------------


def _with(path: list[str], value: Any) -> dict[str, Any]:
    source = _vault_source()
    node = source
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return source


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (_with(["x-infra2", "routes"], {}), r"x-infra2: unknown key\(s\) \['routes'\]"),
        (
            _with(["x-infra2", "vault", "image"], "x"),
            r"x-infra2.vault: unknown key\(s\) \['image'\]",
        ),
        (
            _with(["services", "api", "x-vault"], {"image": "x"}),
            r"services.api.x-vault: unknown key\(s\) \['image'\]",
        ),
        (
            _with(["services", "web", "x-routes"], {}),
            r"services.web: \['x-routes'\] is not supported",
        ),
        (
            _with(["services", "web", "x-capabilities"], {}),
            r"\['x-capabilities'\] is not supported",
        ),
        (_with(["services", "web", "x-preview"], {}), r"\['x-preview'\] is not supported"),
        (_with(["x-infra2", "vault", "name"], ""), r"vault.name must be a non-empty string"),
        (_with(["services", "api", "command"], "run-api"), r"needs `command` as a non-empty list"),
        (_with(["services", "api", "entrypoint"], ["x"]), r"x-vault owns `entrypoint`"),
        (
            _with(["services", "api", "x-vault"], {"max_wait_seconds": 0}),
            r"max_wait_seconds must be a positive integer",
        ),
        (_with(["services", "vault-agent"], {"image": "x"}), r"'vault-agent' is reserved"),
        (_with(["services", "api", "environment"], ["A=1"]), r"environment must be a mapping"),
    ],
)
def test_invalid_source_fails_closed(source: dict[str, Any], message: str) -> None:
    with pytest.raises(ComposeCompileError, match=message):
        compile_compose(source, vault_template="T")


def test_missing_template_missing_vault_block_and_missing_marker_fail_closed() -> None:
    with pytest.raises(ComposeCompileError, match="pass a non-empty vault_template"):
        compile_compose(_vault_source())
    no_block = _vault_source()
    del no_block["x-infra2"]["vault"]
    with pytest.raises(ComposeCompileError, match=r"x-vault used by \['api', 'worker'\]"):
        compile_compose(no_block, vault_template="T")
    with pytest.raises(ComposeCompileError, match="x-infra2 must be a mapping"):
        compile_compose({"services": {}})
    with pytest.raises(ComposeCompileError, match="YAML parse error"):
        compile_compose("a: [")


# --- inspect_compose accepts raw and x-infra2 input ---------------------------------------


def test_inspect_compose_reads_x_infra2_input_with_sidecar_and_missing_limit() -> None:
    source = _vault_source()
    del source["services"]["web"]["mem_limit"]
    report = inspect_compose(yaml.safe_dump(source))
    assert report.errors == ()
    assert report.compliant_services == ("api", "vault-agent", "worker")
    assert report.unlimited_services == ("web",)


def test_inspect_compose_still_reads_raw_input() -> None:
    raw = "services:\n  a:\n    image: x:1\n    mem_limit: 1g\n  b:\n    image: y:1\n"
    report = inspect_compose(raw)
    assert (report.compliant_services, report.unlimited_services) == (("a",), ("b",))


def test_inspect_compose_reports_compile_error_for_bad_x_infra2_input() -> None:
    source = _vault_source()
    source["x-infra2"]["routes"] = {}
    report = inspect_compose(yaml.safe_dump(source))
    assert not report.is_valid
    assert "x-infra2: unknown key(s) ['routes']" in report.errors[0]
