"""platform/30.todo asks the SDK to require the issued telemetry identity (infra2-sdk#105)."""

from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app.py"


def _tree() -> ast.Module:
    return ast.parse(APP.read_text(encoding="utf-8"))


def test_todo_init_service_requires_the_issued_identity() -> None:
    calls = [
        node
        for node in ast.walk(_tree())
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "init_service"
    ]
    assert len(calls) == 1
    keywords = {kw.arg: kw.value for kw in calls[0].keywords}
    assert isinstance(keywords.get("require_issued_identity"), ast.Constant)
    assert keywords["require_issued_identity"].value is True


def test_todo_has_no_local_identity_check() -> None:
    names = {node.name for node in ast.walk(_tree()) if isinstance(node, ast.FunctionDef)}
    assert "_require_issued_telemetry_identity" not in names
