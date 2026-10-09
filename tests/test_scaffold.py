"""Tests for microservice scaffolder and AST facet compliance."""

import ast
from pathlib import Path

from infra2_sdk.scaffold import scaffold_service


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
