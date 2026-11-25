"""Tests for `compile_compose`: baseline defaults and the `x-vault` sidecar expansion.

The fixtures in `tests/fixtures/compose/` come from `wangzitian0/infra2@e276d9a7`
`platform/30.todo/`. Regenerate the golden output with:

    uv run python -c "from pathlib import Path; from infra2_sdk.rules import compile_compose; \
d = Path('tests/fixtures/compose'); \
(d / 'todo.compiled.compose.yaml').write_text(compile_compose( \
(d / 'todo.x-infra2.compose.yaml').read_text(), \
vault_template=(d / 'todo.secrets.ctmpl').read_text().split('\\n', 1)[1]))"

The route fixtures in `tests/fixtures/compose_routes/` are verbatim copies of infra2 compose
files at the commit named in their first line.

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
    # Normalization 2: label order has no meaning to Traefik, so compare labels as sets.
    for spec in (hand, compiled):
        spec["labels"] = sorted(spec["labels"])
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


def test_compiled_sidecar_passes_env_suffix_through_from_the_deploy_env() -> None:
    env = yaml.safe_load(compile_compose(_vault_source(), vault_template="T"))["services"][
        "vault-agent"
    ]["environment"]
    # Same value form as the hand-written infra2 sidecars: an unset suffix gives "".
    assert env["ENV_SUFFIX"] == "${ENV_SUFFIX:-}"


def test_compiled_todo_sidecar_env_equals_handwritten_env() -> None:
    hand = yaml.safe_load(_read("todo.handwritten.compose.yaml"))["services"]["vault-agent"]
    compiled = _compiled_todo()["services"]["vault-agent"]["environment"]
    # The two keys that carry the config and template files are the only intended additions.
    extra = {"VAULT_AGENT_HCL", "VAULT_SECRETS_TEMPLATE"}
    assert extra <= set(compiled)
    assert {k: v for k, v in compiled.items() if k not in extra} == hand["environment"]


def test_compiled_todo_sidecar_supplies_every_env_name_the_template_reads() -> None:
    read = set(re.findall(r'\benv "([A-Z_][A-Z0-9_]*)"', _body("todo.secrets.ctmpl")))
    assert read == {"ENV", "ENV_SUFFIX"}
    assert read <= set(_compiled_todo()["services"]["vault-agent"]["environment"])


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
            _with(["services", "web", "x-capabilities"], {}),
            r"x-capabilities must name at least one capability",
        ),
        (
            _with(["services", "web", "x-preview"], {"keep": True}),
            r"services.web.x-preview: unknown key\(s\) \['keep'\]",
        ),
        (_with(["x-infra2", "vault", "name"], ""), r"vault.name must be a non-empty string"),
        (_with(["services", "api", "command"], "run-api"), r"needs `command` as a non-empty list"),
        (_with(["services", "api", "entrypoint"], ["x"]), r"x-vault owns `entrypoint`"),
        (
            _with(["services", "api", "x-vault"], {"max_wait_seconds": 0}),
            r"max_wait_seconds must be a positive integer",
        ),
        (_with(["services", "vault-agent"], {"image": "x"}), r"'vault-agent' is reserved"),
        (_with(["services", "api", "environment"], ["A=1"]), r"environment must be a mapping"),
        (_with(["services", "web", "x-routes"], []), r"services.web.x-routes must be a mapping"),
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


# --- x-routes -----------------------------------------------------------------------------

ROUTES = Path(__file__).with_name("fixtures") / "compose_routes"
SUFFIX = "${ENV_DOMAIN_SUFFIX}"


def _traefik_labels(spec: dict[str, Any]) -> list[str]:
    return [label for label in spec["labels"] if label.startswith("traefik.")]


def _route_source(**options: Any) -> dict[str, Any]:
    base = {"service": "svc", "port": 80, "routes": [{"name": "r", "rule": "Host(`h`)"}]}
    return _minimal(**{"x-routes": {**base, **options}})


def _route_labels(**options: Any) -> list[str]:
    out = yaml.safe_load(compile_compose(_route_source(**options)))
    return _traefik_labels(out["services"]["app"])


def _compiled_routes(name: str, routes: dict[str, Any]) -> dict[str, Any]:
    hand = yaml.safe_load((ROUTES / name).read_text(encoding="utf-8"))["services"]
    source = {
        "x-infra2": {},
        "services": {
            svc: {"image": "x", "mem_limit": "64m", "x-routes": block}
            for svc, block in routes.items()
        },
    }
    out = yaml.safe_load(compile_compose(source))["services"]
    # Label order has no meaning to Traefik, so compare as sets. A list equality here would
    # also fail on the middleware blocks that the hand files place between routers.
    # Equal length proves that the compiler emits no duplicate label.
    for svc in routes:
        got, want = _traefik_labels(out[svc]), _traefik_labels(hand[svc])
        assert len(got) == len(want)
        assert set(got) == set(want)
    return out


def test_compiled_finance_report_routes_equal_handwritten_labels() -> None:
    host = "Host(`report${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`)"
    _compiled_routes(
        "finance_report.handwritten.compose.yaml",
        {
            "backend": {
                "service": "finance-report-api",
                "port": 8000,
                "routes": [
                    {
                        "name": "finance-report-api",
                        "rule": f"{host} && PathPrefix(`/api`)",
                        "strip_prefix": "/api",
                    }
                ],
            },
            "frontend": {
                "service": "finance-report-web",
                "port": 3000,
                "routes": [{"name": "finance-report-web", "rule": host, "priority": 1}],
            },
        },
    )


def test_compiled_prefect_sso_route_equals_handwritten_labels() -> None:
    _compiled_routes(
        "prefect.handwritten.compose.yaml",
        {
            "prefect-server": {
                "service": "prefect",
                "port": 4200,
                "routes": [
                    {
                        "name": "prefect",
                        "rule": "Host(`prefect${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}`)",
                        "tls": True,
                        "sso": "prefect-auth",
                    }
                ],
            }
        },
    )


def test_x_routes_is_removed_from_the_service_and_other_keys_stay() -> None:
    spec = yaml.safe_load(compile_compose(_route_source()))["services"]["app"]
    assert "x-routes" not in spec
    assert spec["image"] == "alpine:3.20"
    assert spec["restart"] == "always"


def test_route_emits_exact_baseline_label_set() -> None:
    assert sorted(_route_labels()) == sorted(
        [
            "traefik.enable=true",
            f"traefik.http.services.svc{SUFFIX}.loadbalancer.server.port=80",
            f"traefik.http.routers.r{SUFFIX}.rule=Host(`h`)",
            f"traefik.http.routers.r{SUFFIX}.entrypoints=websecure",
            f"traefik.http.routers.r{SUFFIX}.tls.certresolver=letsencrypt",
            f"traefik.http.routers.r{SUFFIX}.service=svc{SUFFIX}",
        ]
    )


def test_route_labels_keep_interpolation_unescaped() -> None:
    labels = _route_labels()
    assert not [label for label in labels if "$$" in label]
    assert all(SUFFIX in label for label in labels if ".routers." in label)


def test_route_tls_priority_strip_and_sso_fields_emit_their_labels() -> None:
    routes = [
        {"name": "a", "rule": "A", "tls": True, "priority": 7, "strip_prefix": "/api"},
        {"name": "b", "rule": "B", "sso": "auth"},
        {"name": "c", "rule": "C", "sso": "auth"},
    ]
    labels = _route_labels(routes=routes)
    assert f"traefik.http.routers.a{SUFFIX}.tls=true" in labels
    assert f"traefik.http.routers.a{SUFFIX}.priority=7" in labels
    assert f"traefik.http.middlewares.a-strip{SUFFIX}.stripprefix.prefixes=/api" in labels
    assert f"traefik.http.routers.a{SUFFIX}.middlewares=a-strip{SUFFIX}@docker" in labels
    assert f"traefik.http.routers.b{SUFFIX}.middlewares=auth{SUFFIX}@docker" in labels
    assert f"traefik.http.routers.c{SUFFIX}.middlewares=auth{SUFFIX}@docker" in labels
    assert f"traefik.http.routers.b{SUFFIX}.tls=true" not in labels
    assert f"traefik.http.routers.b{SUFFIX}.priority=7" not in labels
    # Two routes share the `auth` middleware: it is defined once.
    defined = [x for x in labels if x.startswith(f"traefik.http.middlewares.auth{SUFFIX}.")]
    assert len(defined) == 3
    assert any(x.endswith(".forwardauth.trustForwardHeader=true") for x in defined)


def test_route_labels_append_after_existing_non_traefik_labels() -> None:
    source = _route_source()
    source["services"]["app"]["labels"] = ["iac.x=1"]
    labels = yaml.safe_load(compile_compose(source))["services"]["app"]["labels"]
    assert labels[0] == "iac.x=1"
    assert "traefik.enable=true" in labels[1:]


def test_route_compile_does_not_mutate_its_input_and_is_deterministic() -> None:
    source = _route_source()
    before = copy.deepcopy(source)
    first = compile_compose(source)
    assert source == before
    assert compile_compose(copy.deepcopy(source)) == first


def _routes_with(path: list[str], value: Any) -> dict[str, Any]:
    source = _route_source()
    node: Any = source["services"]["app"]["x-routes"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return source


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (_routes_with(["extra"], 1), r"x-routes: unknown key\(s\) \['extra'\]"),
        (_routes_with(["service"], ""), r"x-routes.service must be a non-empty string"),
        (_routes_with(["port"], 0), r"x-routes.port must be a positive integer"),
        (_routes_with(["port"], True), r"x-routes.port must be a positive integer"),
        (_routes_with(["routes"], []), r"x-routes.routes must be a non-empty list"),
        (_routes_with(["routes"], ["r"]), r"routes\[0\] must be a mapping"),
        (
            _routes_with(["routes"], [{"name": "r", "rule": "H", "max_request_body_bytes": 1}]),
            r"routes\[0\]: unknown key\(s\) \['max_request_body_bytes'\]",
        ),
        (_routes_with(["routes"], [{"rule": "H"}]), r"routes\[0\].name must be a non-empty"),
        (_routes_with(["routes"], [{"name": "r"}]), r"routes\[0\].rule must be a non-empty"),
        (
            _routes_with(["routes"], [{"name": "r", "rule": "H"}, {"name": "r", "rule": "H"}]),
            r"name 'r' is used twice",
        ),
        (
            _routes_with(["routes"], [{"name": "r", "rule": "H", "tls": "yes"}]),
            r"tls must be a boolean",
        ),
        (
            _routes_with(["routes"], [{"name": "r", "rule": "H", "priority": "1"}]),
            r"priority must be an integer",
        ),
        (
            _routes_with(["routes"], [{"name": "r", "rule": "H", "strip_prefix": "api"}]),
            r"strip_prefix must start with `/`",
        ),
        (
            _routes_with(["routes"], [{"name": "r", "rule": "H", "sso": ""}]),
            r"sso must be a non-empty string",
        ),
        (
            _routes_with(
                ["routes"], [{"name": "r", "rule": "H", "sso": "a", "strip_prefix": "/a"}]
            ),
            r"strip_prefix and sso together are not supported",
        ),
    ],
)
def test_invalid_x_routes_fails_closed(source: dict[str, Any], message: str) -> None:
    with pytest.raises(ComposeCompileError, match=message):
        compile_compose(source)


def test_x_routes_rejects_traefik_labels_and_mapping_labels_in_the_source() -> None:
    for labels, message in (
        (["traefik.enable=true"], r"owns every `traefik.\*` label"),
        ({"a": "b"}, r"`labels` must be a list"),
    ):
        source = _route_source()
        source["services"]["app"]["labels"] = labels
        with pytest.raises(ComposeCompileError, match=message):
            compile_compose(source)


# --------------------------------------------------------------------- x-capabilities


def _caps(capabilities: Any, **service: Any) -> dict[str, Any]:
    compiled = yaml.safe_load(
        compile_compose(_minimal(**service, **{"x-capabilities": capabilities}))
    )
    return compiled["services"]["app"]["environment"]


def test_each_capability_emits_the_env_block_the_apps_carry_today() -> None:
    """Values match finance_report and truealpha compose files at infra2@f9c9f8b5."""
    env = _caps(
        {
            "s3": {"public": True},
            "openpanel": {"script": True},
            "otel": {"service_name": "finance-report-backend"},
            "browser_otel": None,
        }
    )
    assert env == {
        "S3_ENDPOINT": "http://platform-s3${ENV_SUFFIX}:9000",
        "S3_PUBLIC_ENDPOINT": "https://s3${ENV_DOMAIN_SUFFIX}.${INTERNAL_DOMAIN}",
        "OPENPANEL_CLIENT_ID": "${OPENPANEL_CLIENT_ID:-}",
        "OPENPANEL_API_URL": "https://openpanel.${INTERNAL_DOMAIN}/api",
        "OPENPANEL_ENVIRONMENT": "${OPENPANEL_ENVIRONMENT:-${ENV}}",
        "OPENPANEL_SCRIPT_URL": "https://openpanel.${INTERNAL_DOMAIN}/op1.js",
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://platform-signoz-otel-collector:4318",
        "OTEL_SERVICE_NAME": "${OTEL_SERVICE_NAME:-finance-report-backend}",
        "OTEL_RESOURCE_ATTRIBUTES": "${OTEL_RESOURCE_ATTRIBUTES:-}",
        "NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT": "${NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT:-}",
        "NEXT_PUBLIC_DEPLOYMENT_ENVIRONMENT": "${ENV}",
        "NEXT_PUBLIC_GIT_SHA": "${GIT_COMMIT_SHA:-unknown}",
    }


def test_a_list_form_uses_the_defaults() -> None:
    env = _caps(["s3", "openpanel"])
    assert "S3_PUBLIC_ENDPOINT" not in env and "OPENPANEL_SCRIPT_URL" not in env
    assert env["S3_ENDPOINT"] == "http://platform-s3${ENV_SUFFIX}:9000"


def test_a_worker_exports_only_when_the_deploy_issued_its_identity() -> None:
    """finance_report prefect-worker (#1431): own name and attribute variables."""
    env = _caps(
        {
            "otel": {
                "service_name": "finance-report-worker",
                "service_name_var": "OTEL_WORKER_SERVICE_NAME",
                "attributes_var": "OTEL_WORKER_RESOURCE_ATTRIBUTES",
                "export": "when_issued",
            }
        }
    )
    assert env == {
        "OTEL_EXPORTER_OTLP_ENDPOINT": (
            "${OTEL_WORKER_RESOURCE_ATTRIBUTES:+http://platform-signoz-otel-collector:4318}"
        ),
        "OTEL_SERVICE_NAME": "${OTEL_WORKER_SERVICE_NAME:-finance-report-worker}",
        "OTEL_RESOURCE_ATTRIBUTES": "${OTEL_WORKER_RESOURCE_ATTRIBUTES:-}",
    }


def test_the_service_keeps_its_own_env_and_an_equal_value_is_allowed() -> None:
    env = _caps(
        ["s3"],
        environment={
            "S3_BUCKET": "statements",
            "S3_ENDPOINT": "http://platform-s3${ENV_SUFFIX}:9000",
        },
    )
    assert env["S3_BUCKET"] == "statements"
    assert env["S3_ENDPOINT"] == "http://platform-s3${ENV_SUFFIX}:9000"


@pytest.mark.parametrize(
    ("capabilities", "service", "message"),
    [
        (
            ["s3"],
            {"environment": {"S3_ENDPOINT": "http://other:9000"}},
            r"set by the service and by the capability",
        ),
        (["prefect"], {}, r"unknown capability 'prefect'"),
        (["s3", "s3"], {}, r"listed twice"),
        ([1], {}, r"capability names only"),
        ({"s3": {"bucket": "x"}}, {}, r"s3: unknown key\(s\) \['bucket'\]"),
        ({"s3": {"public": "yes"}}, {}, r"public must be true or false"),
        ({"otel": {"export": "sometimes"}}, {}, r"export must be 'always' or 'when_issued'"),
        ({"otel": {"attributes_var": "lower"}}, {}, r"attributes_var must be an UPPER_CASE"),
        ({"otel": {"service_name": 3}}, {}, r"service_name must be a string"),
        (["s3"], {"environment": ["A=1"]}, r"`environment` must be a mapping"),
        ("s3", {}, r"x-capabilities must be a mapping"),
    ],
)
def test_invalid_capabilities_fail_closed(capabilities: Any, service: dict, message: str) -> None:
    with pytest.raises(ComposeCompileError, match=message):
        compile_compose(_minimal(**service, **{"x-capabilities": capabilities}))


def test_capability_output_is_byte_deterministic() -> None:
    source = _minimal(**{"x-capabilities": {"otel": {"service_name": "a"}, "s3": None}})
    assert compile_compose(source) == compile_compose(copy.deepcopy(source))


# ------------------------------------------------------------------ preview mode (Slice 4)


def _preview_source() -> dict[str, Any]:
    """finance_report shape: backend on Vault with its own DB in preview, a worker that
    preview drops, a frontend that only takes the preview ENVIRONMENT."""
    return {
        "x-infra2": {
            "vault": {"name": "finance_report"},
            "preview": {"db_container": "finance_report-preview-db${ENV_SUFFIX}"},
        },
        "services": {
            "backend": {
                "image": "backend:1",
                "mem_limit": "512m",
                "command": ["uvicorn", "src.main:app"],
                "x-vault": {},
                "x-preview": {"database_url": "postgresql+asyncpg"},
            },
            "worker": {
                "image": "backend:1",
                "mem_limit": "512m",
                "command": ["prefect", "worker"],
                "x-vault": {},
                "x-preview": {"remove": True},
            },
            "frontend": {
                "image": "frontend:1",
                "mem_limit": "256m",
                "command": ["node", "server.js"],
                "x-vault": {},
                "x-preview": {"environment": True},
            },
        },
    }


def _compile(source: dict[str, Any], mode: str) -> dict[str, Any]:
    return yaml.safe_load(compile_compose(source, vault_template="T\n", mode=mode))


def test_deploy_mode_drops_x_preview_and_adds_no_database() -> None:
    compiled = _compile(_preview_source(), "deploy")
    assert set(compiled["services"]) == {"vault-agent", "backend", "worker", "frontend"}
    assert "preview_db" not in compiled.get("volumes", {})
    script = compiled["services"]["backend"]["entrypoint"][2]
    assert "DATABASE_URL" not in script and "ENVIRONMENT" not in script


def test_preview_mode_adds_the_db_and_overrides_after_the_secrets_are_sourced() -> None:
    compiled = _compile(_preview_source(), "preview")
    services = compiled["services"]
    assert set(services) == {"vault-agent", "backend", "frontend", "db"}
    db = services["db"]
    assert db["image"] == "postgres:16-alpine"
    assert db["container_name"] == "finance_report-preview-db${ENV_SUFFIX}"
    assert db["volumes"] == ["preview_db:/var/lib/postgresql/data"]
    assert compiled["volumes"]["preview_db"] == {}
    backend = services["backend"]
    script = backend["entrypoint"][2]
    source_at = script.index(". /secrets/.env")
    url = (
        "postgresql+asyncpg://ephemeral:ephemeral@"
        "finance_report-preview-db$${ENV_SUFFIX}:5432/preview"
    )
    assert script.index(f'export DATABASE_URL="{url}"') > source_at
    assert script.index('export ENVIRONMENT="$$ENV"') > source_at
    assert script.index('export ENVIRONMENT="$$ENV"') < script.index('exec "$$@"')
    assert backend["depends_on"] == {
        "vault-agent": {"condition": "service_started"},
        "db": {"condition": "service_healthy"},
    }
    frontend_script = services["frontend"]["entrypoint"][2]
    assert 'export ENVIRONMENT="$$ENV"' in frontend_script
    assert "DATABASE_URL" not in frontend_script


def test_preview_output_is_byte_deterministic() -> None:
    assert compile_compose(_preview_source(), vault_template="T\n", mode="preview") == (
        compile_compose(copy.deepcopy(_preview_source()), vault_template="T\n", mode="preview")
    )


def test_the_preview_db_image_is_one_constant() -> None:
    from infra2_sdk.rules import compose as rules_compose

    assert rules_compose.PREVIEW_DB_IMAGE == "postgres:16-alpine"
    text = Path(rules_compose.__file__).read_text(encoding="utf-8")
    assert text.count("postgres:16-alpine") == 1


def _preview_with(path: list[str], value: Any) -> dict[str, Any]:
    source = _preview_source()
    node: Any = source
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return source


@pytest.mark.parametrize(
    ("source", "mode", "message"),
    [
        (
            _preview_with(["services", "backend", "x-preview"], {"database_url": "mysql"}),
            "preview",
            r"must be a postgresql URL scheme",
        ),
        (
            _preview_with(
                ["services", "worker", "x-preview"], {"remove": True, "environment": True}
            ),
            "preview",
            r"`remove` takes no other key",
        ),
        (
            _preview_with(["services", "frontend", "x-preview"], {"environment": "yes"}),
            "preview",
            r"environment must be true or false",
        ),
        (
            _preview_with(["x-infra2", "preview"], {"db_container": ""}),
            "preview",
            r"db_container must be a non-empty string",
        ),
        (
            _preview_with(["x-infra2", "preview"], {"image": "x"}),
            "preview",
            r"x-infra2.preview: unknown key\(s\) \['image'\]",
        ),
        (
            _preview_with(["services", "frontend", "depends_on"], ["worker"]),
            "preview",
            r"depends on \['worker'\], which x-preview removes",
        ),
        (
            _preview_with(["services", "db"], {"image": "x", "mem_limit": "64m"}),
            "preview",
            r"'db' is reserved in preview",
        ),
        (_preview_source(), "staging", r"mode must be one of \['deploy', 'preview'\]"),
    ],
)
def test_invalid_preview_input_fails_closed(source: dict, mode: str, message: str) -> None:
    with pytest.raises(ComposeCompileError, match=message):
        compile_compose(source, vault_template="T\n", mode=mode)


def test_a_database_url_needs_the_db_container_and_x_vault() -> None:
    source = _preview_source()
    del source["x-infra2"]["preview"]
    with pytest.raises(ComposeCompileError, match=r"needs x-infra2.preview.db_container"):
        compile_compose(source, vault_template="T\n", mode="preview")
    source = _preview_source()
    del source["services"]["backend"]["x-vault"]
    with pytest.raises(ComposeCompileError, match=r"it needs x-vault"):
        compile_compose(source, vault_template="T\n", mode="preview")


# --- x-infra2.vault.env forwards named host values to the sidecar (infra2#1822) -----------

# The data_engine template of wangzitian0/infra2 truealpha/truealpha/20.data_engine/secrets.ctmpl.
_PORT_TEMPLATE = (
    '{{- $env := or (env "ENVIRONMENT") (env "ENV") -}}\n'
    '{{- with secret (printf "secret/data/truealpha/%s/postgres" $env) }}\n'
    'DATABASE_URL={{ printf "%q" (printf "postgresql://postgres:%s@127.0.0.1:%s/truealpha"'
    ' .Data.data.POSTGRES_PASSWORD (env "TA_POSTGRES_PORT")) }}\n'
    "{{- end }}\n"
)
_FIXED_ENV = [
    "VAULT_ADDR",
    "VAULT_ROLE_ID",
    "VAULT_SECRET_ID",
    "ENV",
    "ENV_SUFFIX",
    "CONFIGURATION_SHA256",
]


def _sidecar_env_of(env: Any, template: str = "T") -> dict[str, Any]:
    source = _with(["x-infra2", "vault", "env"], env)
    return yaml.safe_load(compile_compose(source, vault_template=template))["services"][
        "vault-agent"
    ]["environment"]


def test_vault_env_names_compile_after_the_fixed_keys_in_the_given_order() -> None:
    env = _sidecar_env_of(["TA_POSTGRES_PORT", "B_PORT", "A_PORT"], _PORT_TEMPLATE)
    assert list(env) == [
        *_FIXED_ENV,
        "TA_POSTGRES_PORT",
        "B_PORT",
        "A_PORT",
        "VAULT_AGENT_HCL",
        "VAULT_SECRETS_TEMPLATE",
    ]
    assert env["TA_POSTGRES_PORT"] == "${TA_POSTGRES_PORT}"
    assert env["B_PORT"] == "${B_PORT}"
    assert env["A_PORT"] == "${A_PORT}"


def test_vault_env_names_reach_only_the_sidecar() -> None:
    doc = yaml.safe_load(
        compile_compose(
            _with(["x-infra2", "vault", "env"], ["TA_POSTGRES_PORT"]), vault_template="T"
        )
    )
    for name in ("api", "worker", "web"):
        assert "TA_POSTGRES_PORT" not in doc["services"][name].get("environment", {})


def test_a_stack_without_vault_env_compiles_to_the_bytes_of_today() -> None:
    # The golden file predates `x-infra2.vault.env`; the todo source has no `env` key.
    assert "env" not in yaml.safe_load(_read("todo.x-infra2.compose.yaml"))["x-infra2"]["vault"]
    text = compile_compose(
        _read("todo.x-infra2.compose.yaml"), vault_template=_body("todo.secrets.ctmpl")
    )
    assert text == _read("todo.compiled.compose.yaml")
    env = _compiled_todo()["services"]["vault-agent"]["environment"]
    assert list(env) == [*_FIXED_ENV, "VAULT_AGENT_HCL", "VAULT_SECRETS_TEMPLATE"]


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ("TA_POSTGRES_PORT", r"x-infra2.vault.env must be a non-empty list of variable names"),
        ({"TA_POSTGRES_PORT": 1}, r"x-infra2.vault.env must be a non-empty list"),
        ([], r"x-infra2.vault.env must be a non-empty list"),
        (["ta_port"], r"x-infra2.vault.env\[0\] 'ta_port' must be an UPPER_CASE variable name"),
        (["OK", "1PORT"], r"x-infra2.vault.env\[1\] '1PORT' must be an UPPER_CASE"),
        (["PORT-A"], r"x-infra2.vault.env\[0\] 'PORT-A' must be an UPPER_CASE"),
        (["PORT\n"], r"x-infra2.vault.env\[0\] 'PORT\\n' must be an UPPER_CASE"),
        ([5432], r"x-infra2.vault.env\[0\] 5432 must be an UPPER_CASE"),
        (["A", "B", "A"], r"x-infra2.vault.env\[2\] 'A' is listed twice"),
        *[
            ([key], rf"x-infra2.vault.env\[0\] '{key}' is a fixed sidecar key")
            for key in [*_FIXED_ENV, "VAULT_AGENT_HCL", "VAULT_SECRETS_TEMPLATE"]
        ],
    ],
)
def test_invalid_vault_env_fails_closed_and_names_the_entry(env: Any, message: str) -> None:
    with pytest.raises(ComposeCompileError, match=message):
        _sidecar_env_of(env)


def test_other_vault_keys_still_fail_next_to_env() -> None:
    source = _with(["x-infra2", "vault", "env"], ["TA_POSTGRES_PORT"])
    source["x-infra2"]["vault"]["environment"] = {"A": "1"}
    with pytest.raises(ComposeCompileError, match=r"unknown key\(s\) \['environment'\]"):
        compile_compose(source, vault_template="T")


def test_a_template_env_read_that_the_sidecar_does_not_get_fails_closed() -> None:
    with pytest.raises(
        ComposeCompileError,
        match=r"vault_template reads env \['TA_POSTGRES_PORT'\].*x-infra2.vault.env",
    ):
        compile_compose(_vault_source(), vault_template=_PORT_TEMPLATE)
    with pytest.raises(ComposeCompileError, match=r"reads env \['TA_POSTGRES_PORT'\]"):
        _sidecar_env_of(["OTHER_PORT"], _PORT_TEMPLATE)


def test_an_or_fallback_passes_when_one_branch_is_in_the_sidecar_env() -> None:
    # `or (env "ENVIRONMENT") (env "ENV")` passes: ENV is a fixed key.
    compile_compose(_vault_source(), vault_template='{{ or (env "ENVIRONMENT") (env "ENV") }}\n')
    with pytest.raises(ComposeCompileError, match=r"reads env \['A_ONE\|B_TWO'\]"):
        compile_compose(_vault_source(), vault_template='{{ or (env "A_ONE") (env "B_TWO") }}\n')
    _sidecar_env_of(["B_TWO"], '{{ or (env "A_ONE") (env "B_TWO") }}\n')


def test_each_fixed_key_satisfies_a_template_read() -> None:
    template = "".join(f'{{{{ env "{key}" }}}}\n' for key in _FIXED_ENV)
    compile_compose(_vault_source(), vault_template=template)
