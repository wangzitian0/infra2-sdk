"""Tests for microservice scaffolder and AST facet compliance."""

import ast
import os
import subprocess
import sys
from importlib.metadata import version as installed_version
from pathlib import Path

import pytest
from packaging.version import Version

import infra2_sdk
from infra2_sdk.ci import validate_manifest_offline
from infra2_sdk.scaffold import (
    generate_requirements_txt,
    main,
    scaffold_manifest,
    scaffold_service,
)


def test_scaffold_generates_all_required_files(tmp_path: Path):
    out_dir = tmp_path / "demo_service"
    created = scaffold_service(
        name="demo-service",
        target_dir=out_dir,
        project="apps",
        port=8080,
        template="fastapi",
        db="postgres",
    )

    expected_files = {
        "compose.yaml",
        "deploy.py",
        "secrets.ctmpl",
        "vault-agent.hcl",
        "vault-policy.hcl",
        "entrypoint.sh",
        "Dockerfile",
        "requirements.txt",
        "app.py",
    }
    assert set(created) == expected_files
    for f in expected_files:
        assert (out_dir / f).exists(), f"Missing file: {f}"


def test_scaffold_deploy_py_ast_facet_compliance(tmp_path: Path):
    out_dir = tmp_path / "ast_test"
    scaffold_service("my-microservice", out_dir, project="apps", port=9000)

    deploy_code = (out_dir / "deploy.py").read_text(encoding="utf-8")
    tree = ast.parse(deploy_code)

    # Find the Deployer class definition
    deployer_class = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name.endswith("Deployer"):
            deployer_class = node
            break

    assert deployer_class is not None, "Deployer class not found in deploy.py"

    # Verify literal AST assignments for probes, signals, and secrets
    attributes = {}
    for item in deployer_class.body:
        if isinstance(item, ast.Assign) and len(item.targets) == 1:
            target = item.targets[0]
            if isinstance(target, ast.Name):
                attributes[target.id] = item.value

    assert "probes" in attributes, "Literal 'probes' assignment missing in ClassDef.body"
    assert "signals" in attributes, "Literal 'signals' assignment missing in ClassDef.body"
    assert "secrets" in attributes, "Literal 'secrets' assignment missing in ClassDef.body"

    # Confirm probes is a literal tuple of ProbeFacet calls
    probes_val = attributes["probes"]
    assert isinstance(probes_val, ast.Tuple)
    assert len(probes_val.elts) >= 1
    probe_call = probes_val.elts[0]
    assert isinstance(probe_call, ast.Call)
    assert getattr(probe_call.func, "id", "") == "ProbeFacet"

    # Confirm signals is a literal tuple of SignalFacet calls
    signals_val = attributes["signals"]
    assert isinstance(signals_val, ast.Tuple)
    assert len(signals_val.elts) >= 1
    signal_call = signals_val.elts[0]
    assert isinstance(signal_call, ast.Call)
    assert getattr(signal_call.func, "id", "") == "SignalFacet"


def test_scaffold_compose_yaml_resource_limits(tmp_path: Path):
    out_dir = tmp_path / "compose_test"
    scaffold_service("web-app", out_dir, project="apps")

    compose_text = (out_dir / "compose.yaml").read_text(encoding="utf-8")

    # Assert no bare ':latest' image tags
    assert ":latest" not in compose_text
    assert "hashicorp/vault:1.15" in compose_text

    # Assert memory ceilings are declared
    assert "memory: 128M" in compose_text
    assert "memory: 256M" in compose_text

    # Assert tmpfs volume for secrets
    assert "type: tmpfs" in compose_text
    assert 'o: "size=10m,mode=0755"' in compose_text

    # Assert health check start_period is at least 120s (for DB migration safety)
    assert "start_period: 120s" in compose_text


def test_scaffold_compose_yaml_db_environment(tmp_path: Path):
    out_pg = tmp_path / "pg_svc"
    scaffold_service("pg-app", out_pg, db="postgres")
    compose_pg = (out_pg / "compose.yaml").read_text(encoding="utf-8")
    assert "DATABASE_URL: ${DATABASE_URL:-}" in compose_pg

    out_none = tmp_path / "none_svc"
    scaffold_service("none-app", out_none, db="none")
    compose_none = (out_none / "compose.yaml").read_text(encoding="utf-8")
    assert "DATABASE_URL" not in compose_none


def test_scaffold_main_cli_with_argv(tmp_path: Path):
    out = tmp_path / "argv_test"
    code = main(["argv-svc", "--out-dir", str(out), "--port", "8088"])
    assert code == 0
    assert (out / "compose.yaml").exists()
    assert (out / "deploy.py").exists()


def test_scaffold_standalone_profile(tmp_path: Path):
    out_dir = tmp_path / "standalone_service"
    created = scaffold_service(
        name="standalone-demo",
        target_dir=out_dir,
        project="apps",
        port=8080,
        template="fastapi",
        db="postgres",
        profile="standalone",
    )

    expected_files = {
        "compose.yaml",
        "entrypoint.sh",
        "Dockerfile",
        "requirements.txt",
        ".env.example",
        "app.py",
    }
    assert set(created) == expected_files
    for f in expected_files:
        assert (out_dir / f).exists(), f"Missing file: {f}"

    # Verify no infra2-specific vault or deployer files in standalone profile
    assert not (out_dir / "deploy.py").exists()
    assert not (out_dir / "vault-agent.hcl").exists()
    assert not (out_dir / "vault-policy.hcl").exists()
    assert not (out_dir / "secrets.ctmpl").exists()

    compose_text = (out_dir / "compose.yaml").read_text(encoding="utf-8")
    assert "dokploy-network" not in compose_text
    assert "vault-agent" not in compose_text
    assert "env_file:" in compose_text

    entrypoint_text = (out_dir / "entrypoint.sh").read_text(encoding="utf-8")
    assert "while [ ! -f /secrets/.env ]" not in entrypoint_text

    env_example = (out_dir / ".env.example").read_text(encoding="utf-8")
    assert "DATABASE_URL=postgresql://" in env_example
    assert "PORT=8080" in env_example


def test_scaffold_app_py_wires_db_healthcheck(tmp_path: Path):
    out_pg = tmp_path / "pg_app"
    scaffold_service("pg-service", out_pg, db="postgres")
    app_pg = (out_pg / "app.py").read_text(encoding="utf-8")
    assert "from infra2_sdk.runtime.postgres import PostgresSettings, probe_postgres" in app_pg
    assert "db_checker=check_db" in app_pg

    out_none = tmp_path / "none_app"
    scaffold_service("none-service", out_none, db="none")
    app_none = (out_none / "app.py").read_text(encoding="utf-8")
    assert "probe_postgres" not in app_none
    assert "db_checker" not in app_none


def test_scaffold_requirements_txt_includes_extras(tmp_path: Path):
    # The expected bound comes from the installed package metadata, never from a literal.
    release = Version(installed_version("infra2-sdk")).base_version

    out_pg = tmp_path / "pg_reqs"
    scaffold_service("pg-reqs", out_pg, db="postgres")
    reqs_pg = (out_pg / "requirements.txt").read_text(encoding="utf-8")
    assert f"infra2-sdk[fastapi,otel,postgres]>={release}\n" in reqs_pg
    # The SDK extra owns the FastAPI version bound: a second line would drift from it.
    assert not [line for line in reqs_pg.splitlines() if line.startswith("fastapi")]

    out_none = tmp_path / "none_reqs"
    scaffold_service("none-reqs", out_none, db="none")
    reqs_none = (out_none / "requirements.txt").read_text(encoding="utf-8")
    assert f"infra2-sdk[fastapi,otel]>={release}\n" in reqs_none


def test_scaffold_requirement_follows_the_sdk_version(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(infra2_sdk, "__version__", "9.8.7")

    assert "infra2-sdk[fastapi,otel,postgres]>=9.8.7\n" in generate_requirements_txt(db="postgres")
    assert "infra2-sdk[fastapi,otel]>=9.8.7\n" in generate_requirements_txt(db="none")


@pytest.mark.parametrize(
    ("sdk_version", "bound"),
    [
        ("3.4.0.dev1", "3.4.0"),
        ("3.4.0.dev12+g1a2b3c4", "3.4.0"),
        ("3.4.0+local", "3.4.0"),
        ("3.4.0rc1", "3.4.0"),
        ("3.4.0.post2", "3.4.0"),
        ("3.4.0", "3.4.0"),
    ],
)
def test_scaffold_requirement_keeps_only_the_release_part(
    monkeypatch: pytest.MonkeyPatch, sdk_version: str, bound: str
):
    monkeypatch.setattr(infra2_sdk, "__version__", sdk_version)

    assert generate_requirements_txt(db="none").endswith(f"infra2-sdk[fastapi,otel]>={bound}\n")


def test_scaffold_requirement_rejects_a_version_without_a_release_part(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(infra2_sdk, "__version__", "not-a-version")

    with pytest.raises(ValueError, match="not-a-version"):
        generate_requirements_txt(db="none")


def test_scaffold_cli_subcommand_forwarding_and_wrapper(tmp_path: Path):
    from infra2_sdk.cli.scaffold import main as cli_main

    # 1. Forwarded 'scaffold' subcommand
    out1 = tmp_path / "sub_test"
    code1 = main(["scaffold", "forwarded-svc", "--out-dir", str(out1), "--profile", "standalone"])
    assert code1 == 0
    assert (out1 / ".env.example").exists()

    # 2. CLI wrapper execution
    out2 = tmp_path / "cli_wrapper_test"
    code2 = cli_main(["scaffold", "wrapped-svc", "--out-dir", str(out2)])
    assert code2 == 0
    assert (out2 / "compose.yaml").exists()


def test_scaffold_secrets_template_comes_from_the_sdk_renderer(tmp_path: Path):
    out_dir = tmp_path / "vault_pg"
    scaffold_service("demo-service", out_dir, project="apps", db="postgres")

    template = (out_dir / "secrets.ctmpl").read_text(encoding="utf-8")

    assert "Generated from scaffold:apps/demo_service by infra2_sdk.secrets" in template
    assert '{{- with secret (printf "secret/data/apps/%s/demo_service" $env) }}' in template
    assert 'DATABASE_URL={{ printf "%q" .Data.data.DATABASE_URL }}' in template
    assert "range $k, $v" not in template, "the hand-written wildcard template is gone"


def test_scaffold_vault_policy_grants_exactly_the_template_reads(tmp_path: Path):
    out_dir = tmp_path / "vault_policy"
    scaffold_service("demo-service", out_dir, project="apps", db="postgres")

    policy = (out_dir / "vault-policy.hcl").read_text(encoding="utf-8")

    assert 'path "secret/data/apps/{{env}}/demo_service" {' in policy
    assert 'path "secret/metadata/apps/{{env}}/demo_service" {' in policy
    assert 'path "auth/token/renew-self" {' in policy
    assert policy.count("capabilities") == 4


def test_scaffold_without_database_declares_no_secret_fields(tmp_path: Path):
    out_dir = tmp_path / "vault_none"
    scaffold_service("demo-service", out_dir, project="apps", db="none")

    template = (out_dir / "secrets.ctmpl").read_text(encoding="utf-8")
    policy = (out_dir / "vault-policy.hcl").read_text(encoding="utf-8")

    assert "DATABASE_URL" not in template
    assert "with secret" not in template
    assert 'path "secret/data/apps/{{env}}/demo_service" {' in policy


@pytest.mark.parametrize("profile", ["infra2", "standalone"])
@pytest.mark.parametrize("db", ["postgres", "none"])
def test_scaffold_dockerfile_starts_an_app_that_exists(tmp_path: Path, profile: str, db: str):
    out_dir = tmp_path / f"docker_{profile}_{db}"
    created = scaffold_service("demo-service", out_dir, profile=profile, db=db)

    dockerfile = (out_dir / "Dockerfile").read_text(encoding="utf-8")

    assert '"uvicorn", "app:app"' in dockerfile
    assert "app.py" in created
    assert (out_dir / "app.py").is_file()


def test_scaffold_generic_template_is_gone(tmp_path: Path, capsys):
    with pytest.raises(ValueError, match="template"):
        scaffold_service("demo-service", tmp_path / "generic", template="generic")
    assert not (tmp_path / "generic").exists(), "a rejected template must not write files"

    with pytest.raises(SystemExit) as exc:
        main(["demo-service", "--template", "generic", "--out-dir", str(tmp_path / "cli")])
    assert exc.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


@pytest.mark.parametrize("db", ["postgres", "none"])
def test_scaffolded_app_promises_only_what_init_service_does(tmp_path: Path, db: str):
    out_dir = tmp_path / f"promises_{db}"
    scaffold_service("demo-service", out_dir, db=db)

    app_text = (out_dir / "app.py").read_text(encoding="utf-8")

    assert "init_service(app" in app_text
    for unkept_promise in ("SIGTERM", "SigNoz", "Prometheus"):
        assert unkept_promise not in app_text


def _init_service_call(app_text: str) -> ast.Call:
    calls = [
        node
        for node in ast.walk(ast.parse(app_text))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "init_service"
    ]
    assert len(calls) == 1, "the generated app must call init_service exactly once"
    return calls[0]


@pytest.mark.parametrize("db", ["postgres", "none"])
def test_standalone_app_passes_strict_otel_false_and_says_why(tmp_path: Path, db: str):
    scaffold_service("demo-service", tmp_path / "app", profile="standalone", db=db)

    app_text = (tmp_path / "app" / "app.py").read_text(encoding="utf-8")

    options = {keyword.arg: keyword.value for keyword in _init_service_call(app_text).keywords}
    assert ast.literal_eval(options["strict_otel"]) is False
    assert "# A laptop sets no ENVIRONMENT, so strict_otel=False lets" in app_text
    # The compose file defaults ENV to local_dev, so the comment says what a deployment must do.
    assert "set ENV to the real tier" in app_text
    assert "removing the keyword is not enough" in app_text


@pytest.mark.parametrize("db", ["postgres", "none"])
def test_infra2_app_keeps_the_strict_default(tmp_path: Path, db: str):
    scaffold_service("demo-service", tmp_path / "app", profile="infra2", db=db)

    app_text = (tmp_path / "app" / "app.py").read_text(encoding="utf-8")

    assert "strict_otel" not in {keyword.arg for keyword in _init_service_call(app_text).keywords}
    assert "strict_otel" not in app_text


def _run_generated_app(tmp_path: Path, profile: str, code: str) -> subprocess.CompletedProcess:
    """Scaffold an app without a database, then run ``code`` in the app directory.

    The child process has no variable except PATH and the path of the SDK under test. It has
    no tier and no network.
    """
    app_dir = tmp_path / profile
    scaffold_service("demo-service", app_dir, profile=profile, db="none")
    sdk_source = str(Path(infra2_sdk.__file__).resolve().parents[1])
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=app_dir,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": sdk_source},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_standalone_app_starts_and_answers_livez_without_a_tier(tmp_path: Path):
    code = (
        "from starlette.testclient import TestClient\n"
        "import app\n"
        "response = TestClient(app.app).get('/livez')\n"
        "print(response.status_code, response.json()['service'])\n"
    )

    result = _run_generated_app(tmp_path, "standalone", code)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "200 demo_service"


def test_infra2_app_stops_at_import_without_a_tier(tmp_path: Path):
    result = _run_generated_app(tmp_path, "infra2", "import app")

    assert result.returncode != 0
    assert "ValueError: ENVIRONMENT is required; set one of ENVIRONMENT, ENV, APP_ENV" in (
        result.stderr
    )


def test_infra2_app_starts_with_a_known_tier(tmp_path: Path):
    code = (
        "import os\n"
        "os.environ['ENV'] = 'local_dev'\n"
        "from starlette.testclient import TestClient\n"
        "import app\n"
        "print(TestClient(app.app).get('/livez').status_code)\n"
    )

    result = _run_generated_app(tmp_path, "infra2", code)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "200"


def test_scaffold_manifest_declares_the_database_url_as_a_required_sensitive_human_secret():
    # The Vault renderers ignore `sensitive`. The flag changes the JSON schema and how
    # validate_environment compares values, so those are the observable outputs.
    manifest = scaffold_manifest("demo-service", "apps", "postgres")

    schema = manifest.to_json_schema()

    assert list(schema["properties"]) == ["DATABASE_URL"]
    assert schema["properties"]["DATABASE_URL"]["x-sensitive"] is True
    assert schema["properties"]["DATABASE_URL"]["x-source"] == "human"
    assert schema["required"] == ["DATABASE_URL"]
    assert validate_manifest_offline(manifest) == []


def test_scaffold_manifest_without_database_declares_no_field():
    manifest = scaffold_manifest("demo-service", "apps", "none")

    assert manifest.fields == ()
    assert manifest.to_json_schema()["properties"] == {}
    assert validate_manifest_offline(manifest) == []


def test_scaffold_vault_agent_healthcheck_needs_a_non_empty_env_file_when_secrets_are_declared(
    tmp_path: Path,
):
    scaffold_service("demo-service", tmp_path / "pg", db="postgres")

    compose = (tmp_path / "pg" / "compose.yaml").read_text(encoding="utf-8")

    assert "test -s /vault/secrets/.env" in compose
    assert "test -e /vault/secrets/.env" not in compose
    assert "test -s /vault/.token" in compose


def test_scaffold_vault_agent_healthcheck_accepts_an_empty_env_file_when_no_secret_is_declared(
    tmp_path: Path,
):
    scaffold_service("demo-service", tmp_path / "none", db="none")

    compose = (tmp_path / "none" / "compose.yaml").read_text(encoding="utf-8")
    template = (tmp_path / "none" / "secrets.ctmpl").read_text(encoding="utf-8")

    assert "with secret" not in template, "the rendered .env is empty"
    assert "test -e /vault/secrets/.env" in compose
    assert "test -s /vault/secrets/.env" not in compose
    assert "test -s /vault/.token" in compose
    assert 'grep -q "<no value>" /vault/secrets/.env' in compose
