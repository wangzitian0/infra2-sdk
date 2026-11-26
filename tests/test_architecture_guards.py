"""Positive architecture and package hygiene guards for infra2-sdk."""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys
import tomllib
from pathlib import Path

import infra2_sdk

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src" / "infra2_sdk"
EXPECTED_MODULES = ("config", "telemetry", "service", "storage", "llm", "delivery")


def test_package_metadata_and_module_surface() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    assert project["version"] == "4.11.1"
    assert infra2_sdk.__all__ == EXPECTED_MODULES

    core_deps = {
        dep.split(">")[0].split("<")[0].split("=")[0].strip() for dep in project["dependencies"]
    }
    assert core_deps == {"pydantic", "pyyaml"}

    public_files = tuple(sorted(p.stem for p in SRC_DIR.glob("*.py") if not p.name.startswith("_")))
    assert set(public_files) == set(EXPECTED_MODULES)
    private_files = tuple(sorted(p.stem for p in SRC_DIR.glob("_*.py") if p.name != "__init__.py"))
    assert private_files == ("_core",)
    assert sorted(p.name for p in SRC_DIR.iterdir() if p.is_dir() and p.name != "__pycache__") == [
        "data"
    ]

    for mod_name in EXPECTED_MODULES:
        mod = importlib.import_module(f"infra2_sdk.{mod_name}")
        exports = tuple(mod.__all__)
        assert len(exports) == len(set(exports)), f"duplicate export in {mod_name}"
        for sym in exports:
            assert hasattr(mod, sym), f"{mod_name}.__all__ lists missing symbol {sym!r}"


def test_no_cross_module_private_imports_and_lazy_import_zero_io() -> None:
    for py_path in SRC_DIR.glob("*.py"):
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("infra2_sdk.")
            ):
                target_leaf = node.module.split(".", 1)[1]
                if target_leaf != "_core":
                    for alias in node.names:
                        assert not alias.name.startswith("_"), (
                            f"{py_path.name} imports private {alias.name} from {node.module}"
                        )

    code = (
        "import infra2_sdk.delivery as d; "
        "assert d.load_stages()['policy_version'] == 1; "
        "assert 'staging.operation' in d.CELL_POLICIES"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    # Specifically ensure importing infra2_sdk.delivery does not invoke yaml.safe_load on import.
    probe = (
        "import yaml; called = []; orig = yaml.safe_load; "
        "yaml.safe_load = lambda *a, **k: (called.append(1), orig(*a, **k))[1]; "
        "import infra2_sdk.delivery; assert called == [], called; "
        "_ = infra2_sdk.delivery.CELL_POLICIES; assert len(called) == 1"
    )
    proc2 = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc2.returncode == 0, proc2.stderr
