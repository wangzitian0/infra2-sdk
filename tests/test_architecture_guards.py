"""Positive architecture and package hygiene guards for infra2-sdk."""

from __future__ import annotations

import ast
import importlib
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import infra2_sdk

ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = ROOT / "src" / "infra2_sdk"
EXPECTED_MODULES = ("config", "telemetry", "service", "storage", "llm", "delivery")
EXPECTED_EXPORTS: dict[str, tuple[str, ...]] = {
    "config": (
        "EnvironmentField",
        "EnvironmentManifest",
        "EnvironmentTier",
        "RuntimeEnvKey",
        "assert_env",
        "read_runtime_env",
        "resolve_tier",
    ),
    "telemetry": (
        "TelemetryProviders",
        "bind_log_context",
        "current_request_id",
        "get_meter",
        "get_tracer",
        "init_worker",
    ),
    "service": (
        "CallableCheck",
        "Dependency",
        "DependencyKind",
        "DependencyManifest",
        "DependencyStatus",
        "HttpClientSettings",
        "ProbeResult",
        "RateLimitWindow",
        "async_request_with_retry",
        "create_async_http_client",
        "create_http_client",
        "init_service",
        "probe_http",
        "request_with_retry",
    ),
    "storage": (
        "PostgresSettings",
        "RedisSettings",
        "S3Settings",
        "create_s3_client",
        "ensure_bucket",
        "is_not_found",
        "probe_postgres",
        "probe_redis",
        "probe_s3",
        "put_object_bytes",
        "read_object_bytes",
        "redact_postgres_error",
        "redact_presigned_url",
        "redact_redis_error",
    ),
    "llm": (
        "DecodingSpec",
        "LlmSettings",
        "create_llm_agent",
        "llm_call_context",
        "prompt_sha256",
        "redact_llm_error",
        "request_sha256",
        "schema_sha256",
    ),
    "delivery": (
        "CELL_POLICIES",
        "CellPolicy",
        "CheckRow",
        "RuntimeHooksManifest",
        "resolve_check_cells",
    ),
}


def test_package_metadata_and_module_surface() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    assert project["version"] == "4.12.0"
    assert infra2_sdk.__all__ == EXPECTED_MODULES
    assert [n for n in dir(infra2_sdk) if not n.startswith("_")] == sorted(infra2_sdk.__all__)

    core_deps = {
        dep.split(">")[0].split("<")[0].split("=")[0].strip() for dep in project["dependencies"]
    }
    assert core_deps == {"pydantic", "pyyaml"}

    opt_deps = project["optional-dependencies"]
    assert "dev" not in opt_deps
    assert "dev" in pyproject.get("dependency-groups", {})
    assert set(opt_deps["telemetry"]) <= set(opt_deps["fastapi"])
    assert set(opt_deps["http"]) <= set(opt_deps["fastapi"])
    assert set(opt_deps["telemetry"]) <= set(opt_deps["llm"])
    assert set(opt_deps["http"]) <= set(opt_deps["llm"])
    for extra_name in ("telemetry", "http", "fastapi", "postgres", "s3", "llm"):
        assert set(opt_deps[extra_name]) <= set(opt_deps["all"]), extra_name

    public_files = tuple(sorted(p.stem for p in SRC_DIR.glob("*.py") if not p.name.startswith("_")))
    assert set(public_files) == set(EXPECTED_MODULES)
    private_files = tuple(sorted(p.stem for p in SRC_DIR.glob("_*.py") if p.name != "__init__.py"))
    assert private_files == ("_core",)
    assert sorted(p.name for p in SRC_DIR.iterdir() if p.is_dir() and p.name != "__pycache__") == [
        "data"
    ]

    total = 0
    for mod_name in EXPECTED_MODULES:
        mod = importlib.import_module(f"infra2_sdk.{mod_name}")
        exports = tuple(mod.__all__)
        assert len(exports) == len(set(exports)), f"duplicate export in {mod_name}"
        assert set(exports) == set(EXPECTED_EXPORTS[mod_name]), (
            f"{mod_name}.__all__ drifted: got {sorted(exports)}, "
            f"expected {sorted(EXPECTED_EXPORTS[mod_name])}"
        )
        assert [n for n in dir(mod) if not n.startswith("_")] == sorted(exports), (
            f"dir(infra2_sdk.{mod_name}) leaks non-exported names"
        )
        for sym in exports:
            assert hasattr(mod, sym), f"{mod_name}.__all__ lists missing symbol {sym!r}"
        total += len(exports)
    assert total == 54


def test_no_unexported_public_defs_cross_private_imports_or_eager_io() -> None:
    for mod_name in EXPECTED_MODULES:
        py_path = SRC_DIR / f"{mod_name}.py"
        tree = ast.parse(py_path.read_text(encoding="utf-8"))
        for stmt in tree.body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                sym_name = stmt.name
            elif isinstance(stmt, ast.Assign):
                targets = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
                sym_name = targets[0] if len(targets) == 1 else ""
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                sym_name = stmt.target.id
            else:
                sym_name = ""
            if sym_name and not sym_name.startswith("_"):
                assert sym_name in EXPECTED_EXPORTS[mod_name], (
                    f"{py_path.name} defines non-exported public symbol {sym_name!r}"
                )

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

    root_probe = (
        "import builtins, importlib.metadata as im; "
        "opened = []; ver_calls = []; "
        "orig_open = builtins.open; orig_ver = im.version; "
        "builtins.open = lambda f, *a, **k: ("
        "opened.append(str(f)) if str(f).endswith(('.toml', '.yaml', '.yml')) else None, "
        "orig_open(f, *a, **k))[1]; "
        "im.version = lambda n: (ver_calls.append(n), orig_ver(n))[1]; "
        "import infra2_sdk; "
        "assert opened == [] and ver_calls == [], (opened, ver_calls); "
        "assert infra2_sdk.__version__ == '4.12.0'; "
        "assert len(ver_calls) == 1"
    )
    proc_root = subprocess.run(
        [sys.executable, "-c", root_probe],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc_root.returncode == 0, proc_root.stderr

    # Importing infra2_sdk.delivery must not invoke yaml.safe_load on import.
    probe = (
        "import yaml; called = []; orig = yaml.safe_load; "
        "yaml.safe_load = lambda *a, **k: (called.append(1), orig(*a, **k))[1]; "
        "import infra2_sdk.delivery; assert called == [], called; "
        "assert 'staging.operation' in infra2_sdk.delivery.CELL_POLICIES; "
        "assert len(called) == 1"
    )
    proc2 = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc2.returncode == 0, proc2.stderr


def test_readme_version_and_module_surface_table_parity() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "v4.12.0" in readme and "4.12.0" in readme
    assert "4.11." not in readme and "4.10." not in readme

    table_rows = re.findall(r"^\|\s*`infra2_sdk\.([a-z]+)`\s*\|[^|]+\|\s*([^|]+)\|", readme, re.M)
    by_mod = {mod: cols for mod, cols in table_rows}
    assert set(by_mod) == set(EXPECTED_MODULES)
    for mod_name, expected in EXPECTED_EXPORTS.items():
        listed = {
            token.rstrip("()")
            for token in re.findall(r"`([^`]+)`", by_mod[mod_name])
            if not token.startswith("python -m ")
        }
        assert listed == set(expected), (
            f"README.md {mod_name}: got {sorted(listed)}, expected {sorted(expected)}"
        )
