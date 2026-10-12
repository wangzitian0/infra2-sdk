"""Tests for microservice scaffolder and AST facet compliance."""

import ast
from pathlib import Path

import pytest

from infra2_sdk.scaffold import main, scaffold_service


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
    out_pg = tmp_path / "pg_reqs"
    scaffold_service("pg-reqs", out_pg, db="postgres")
    reqs_pg = (out_pg / "requirements.txt").read_text(encoding="utf-8")
    assert "infra2-sdk[fastapi,otel,postgres]>=3.1.2" in reqs_pg
    # The SDK extra owns the FastAPI version bound: a second line would drift from it.
    assert not [line for line in reqs_pg.splitlines() if line.startswith("fastapi")]

    out_none = tmp_path / "none_reqs"
    scaffold_service("none-reqs", out_none, db="none")
    reqs_none = (out_none / "requirements.txt").read_text(encoding="utf-8")
    assert "infra2-sdk[fastapi,otel]>=3.1.2" in reqs_none


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
