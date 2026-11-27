"""Tests for infra2_sdk.delivery."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import pytest

from infra2_sdk import delivery


def test_stages_and_hooks_manifest_and_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "staging.operation" in delivery.CELL_POLICIES
    assert isinstance(delivery.CELL_POLICIES["staging.operation"], delivery.CellPolicy)

    smoke_row = delivery.CheckRow(
        id="smoke", argv=("echo", "ok"), needs=("code", "running_service"), writes=False
    )
    assert delivery.resolve_check_cells(smoke_row) == (
        "preview.operation",
        "staging.operation",
        "prod.operation",
    )

    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    v3_dict = {
        "schema_version": 3,
        "service": "finance_report/app",
        "artifacts_dir": ".artifacts/custom",
        "checks": [
            {
                "id": "e2e-write",
                "argv": ["echo", "write"],
                "needs": ["code", "running_service"],
                "writes": True,
                "accepts_scale": False,
                "timeout_seconds": 90,
                "bootstrap": "scripts/boot.py",
                "optional_env": ["OPT_VAR"],
                "checkout_lineage": True,
                "cells": {
                    "remove": {"preview.post": {"reason": "skip preview write", "issue": "repo#1"}}
                },
            },
            {
                "id": "unit",
                "argv": ["pytest"],
                "needs": ["code", "recorded_llm"],
                "cells": {
                    "add": {
                        "preview.post": {
                            "reason": "expired addition",
                            "expires_at": "2026-11-25T00:00:00Z",
                        }
                    }
                },
            },
            {
                "id": "muted",
                "argv": ["echo", "muted"],
                "needs": ["code", "running_service"],
                "off": {"reason": "temp mute", "issue": "repo#2"},
            },
        ],
    }
    v3_manifest = delivery.RuntimeHooksManifest.model_validate(v3_dict)
    rw_cells = delivery.resolve_check_cells(v3_manifest.checks[0], now=now)
    assert "preview.post" not in rw_cells
    assert "prod.post" not in rw_cells
    assert "staging.post" in rw_cells and "scheduled_audit" in rw_cells

    assert set(delivery.resolve_check_cells(v3_manifest.checks[1], now=now)) == {
        "local.pre",
        "local.post",
        "ci.pre",
        "ci.post",
    }

    bad_prod = delivery.CheckRow(
        id="unsafe",
        argv=("echo", "x"),
        needs=("code", "running_service"),
        writes=True,
        cells=delivery._CellOverrides(add={"prod.post": delivery._OverrideReason(reason="unsafe")}),
    )
    with pytest.raises(ValueError, match="cannot run in prod"):
        delivery.resolve_check_cells(bad_prod)

    muted_only = delivery.RuntimeHooksManifest(
        schema_version=3,
        service="demo/muted",
        checks=(v3_manifest.checks[2],),
    )
    assert delivery._run_cell(muted_only, "staging.post", now=now).passed is False

    manifest = delivery.RuntimeHooksManifest(
        schema_version=delivery._HOOKS_SCHEMA_VERSION,
        service="demo/svc",
        required_env=("APP_URL",),
        optional_env=("TARGET_URL",),
        checks=(smoke_row,),
    )
    env = delivery._build_hook_env(
        manifest,
        {"APP_URL": "http://127.0.0.1:8000", "DATABASE_URL": "postgresql://secret", "AWS_KEY": "x"},
        cell="staging.operation",
    )
    assert env["APP_URL"] == "http://127.0.0.1:8000"
    assert (
        env["HOOK_CELL"] == "staging.operation" and env["HOOK_ARTIFACTS_DIR"] == ".artifacts/hooks"
    )
    assert "DATABASE_URL" not in env and "AWS_KEY" not in env

    res = delivery._run_cell(
        manifest,
        "staging.operation",
        environ={"APP_URL": "http://127.0.0.1:8000"},
        runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="ok", stderr=""),
    )
    assert res.passed is True and len(res.verdicts) == 1

    m_path = tmp_path / "hooks.json"
    m_path.write_text(manifest.to_json(), encoding="utf-8")
    assert delivery._main(["check-hooks", str(m_path)]) == 0
    capsys.readouterr()
    monkeypatch.setenv("APP_URL", "http://127.0.0.1:8000")
    assert delivery._main(["run-cell", str(m_path), "--cell", "staging.operation"]) == 0
    cli_out = json.loads(capsys.readouterr().out)
    assert cli_out["passed"] is True and cli_out["verdicts"][0]["passed"] is True

    def _timeout_runner(argv, **_kw):
        raise subprocess.TimeoutExpired(argv, 1, output=b"partial-out", stderr=b"partial-err")

    tout = delivery._run_cell(
        manifest,
        "staging.operation",
        environ={"APP_URL": "http://127.0.0.1:8000"},
        runner=_timeout_runner,
    )
    assert tout.passed is False and tout.verdicts[0].exit_code == 124
    assert tout.verdicts[0].stdout == "partial-out" and "partial-err" in tout.verdicts[0].stderr

    os_err = delivery._run_cell(
        manifest,
        "staging.operation",
        environ={"APP_URL": "http://127.0.0.1:8000"},
        runner=lambda _a, **_k: (_ for _ in ()).throw(FileNotFoundError("no bin")),
    )
    assert os_err.passed is False and os_err.verdicts[0].exit_code == 127


def test_health_polling_and_cli() -> None:
    rolling_responses = [
        *[(200, b'{"version":"old-tag","git_sha":"0000000000000000000000000000000000000000"}')] * 7,
        (200, b'{"version":"v4.12.0","git_sha":"abcdef1234567890"}'),
    ]
    now = [0.0]

    res = delivery._poll_until_healthy(
        "https://todo.zitian.party/api/health",
        expected_version="abcdef1",
        timeout_seconds=30.0,
        interval_seconds=1.0,
        http_get=lambda _u, _t: rolling_responses.pop(0),
        monotonic=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    assert res.ok is True and res.attempts == 8

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/redir":
                self.send_response(302)
                self.send_header("Location", "/ok")
                self.end_headers()
                return
            body = b"plain-ok" if self.path == "/text" else b'{"service_version":"v4.12.0"}'
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert (
            delivery._default_http_get(f"http://127.0.0.1:{srv.server_port}/redir", 2.0)[0] == 302
        )
        assert (
            delivery._main(
                [
                    "poll",
                    "--url",
                    f"http://127.0.0.1:{srv.server_port}/text",
                    "--timeout-seconds",
                    "2",
                ]
            )
            == 0
        )
        assert (
            delivery._main(
                [
                    "poll",
                    "--url",
                    f"http://127.0.0.1:{srv.server_port}/ok",
                    "--expected-version",
                    "v4.12.0",
                    "--timeout-seconds",
                    "2",
                ]
            )
            == 0
        )
    finally:
        srv.shutdown()
        srv.server_close()


def test_default_http_get_sends_sdk_user_agent() -> None:
    seen_headers: list[tuple[str, str]] = []

    class _WafHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            ua = self.headers.get("User-Agent", "")
            accept = self.headers.get("Accept", "")
            seen_headers.append((ua, accept))
            if not ua or ua.startswith("Python-urllib"):
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"cloudflare 403 blocked")
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"healthy","version":"v4.13.0"}')

        def log_message(self, *args: object) -> None:
            return None

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _WafHandler)
    Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, body = delivery._default_http_get(
            f"http://127.0.0.1:{srv.server_port}/api/health",
            2.0,
        )
        assert status == 200
        assert b'"healthy"' in body
        assert len(seen_headers) == 1
        ua, accept = seen_headers[0]
        assert ua.startswith("infra2-sdk/"), f"unexpected User-Agent: {ua!r}"
        assert accept == "application/json"
    finally:
        srv.shutdown()
        srv.server_close()


def test_per_check_required_and_optional_env_scoping() -> None:
    manifest = delivery.RuntimeHooksManifest(
        schema_version=3,
        service="demo/svc",
        required_env=("APP_URL",),
        checks=(
            delivery.CheckRow(
                id="lint",
                argv=("ruff", "check"),
                needs=("code",),
                required_env=("CI_ONLY_REQ",),
                optional_env=("CI_ONLY_OPT",),
            ),
            delivery.CheckRow(
                id="e2e",
                argv=("pytest", "e2e"),
                needs=("code", "running_service"),
                required_env=("POST_ONLY_REQ",),
                optional_env=("POST_ONLY_OPT",),
            ),
        ),
    )

    seen_envs: list[dict[str, str]] = []

    def _capture_runner(argv, **kw):
        seen_envs.append(dict(kw["env"]))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    ci_res = delivery._run_cell(
        manifest,
        "ci.pre",
        environ={
            "APP_URL": "http://127.0.0.1:8000",
            "CI_ONLY_REQ": "yes",
            "CI_ONLY_OPT": "opt-ci",
            "POST_ONLY_OPT": "should-not-pass",
        },
        runner=_capture_runner,
    )
    assert ci_res.passed is True
    assert seen_envs[-1]["CI_ONLY_REQ"] == "yes"
    assert seen_envs[-1]["CI_ONLY_OPT"] == "opt-ci"
    assert "POST_ONLY_OPT" not in seen_envs[-1]


def test_run_cell_executes_bootstrap_and_preserves_checkout_lineage() -> None:
    seen_commands: list[list[str]] = []

    def ok_runner(argv, **_kwargs):
        seen_commands.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    manifest = delivery.RuntimeHooksManifest(
        schema_version=delivery._HOOKS_SCHEMA_VERSION,
        service="demo/svc",
        checks=(
            delivery.CheckRow(
                id="smoke",
                argv=("echo", "run"),
                needs=("code", "running_service"),
                bootstrap="echo prep",
                checkout_lineage=True,
            ),
        ),
    )
    assert manifest.checks[0].checkout_lineage is True
    ok_res = delivery._run_cell(manifest, "staging.operation", environ={}, runner=ok_runner)
    assert ok_res.passed is True
    assert seen_commands == [["sh", "-c", "echo prep"], ["echo", "run"]]

    seen_commands.clear()

    def failing_bootstrap_runner(argv, **_kwargs):
        seen_commands.append(list(argv))
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="pip install failed")

    fail_res = delivery._run_cell(
        manifest, "staging.operation", environ={}, runner=failing_bootstrap_runner
    )
    assert fail_res.passed is False
    assert seen_commands == [["sh", "-c", "echo prep"]]
    assert fail_res.verdicts[0].exit_code == 2
    assert "bootstrap failed: pip install failed" in fail_res.verdicts[0].stderr

    seen_commands.clear()

    def timeout_runner(argv, **kwargs):
        seen_commands.append(list(argv))
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output="partial")

    timeout_res = delivery._run_cell(
        manifest, "staging.operation", environ={}, runner=timeout_runner
    )
    assert timeout_res.passed is False
    assert seen_commands == [["sh", "-c", "echo prep"]]
    assert timeout_res.verdicts[0].exit_code == 124
    assert "bootstrap timed out after" in timeout_res.verdicts[0].stderr


def test_resolve_check_cells_respects_active_off_override() -> None:
    check_off = delivery.CheckRow(
        id="staging_gate",
        argv=("echo", "gate"),
        needs=("code", "running_service"),
        off=delivery._OverrideReason(reason="paused", issue="wangzitian0/infra2#1823"),
    )
    assert delivery.resolve_check_cells(check_off) == ()

    expired = delivery.CheckRow(
        id="staging_gate",
        argv=("echo", "gate"),
        needs=("code", "running_service"),
        off=delivery._OverrideReason(
            reason="paused",
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        ),
    )
    assert "staging.post" in delivery.resolve_check_cells(
        expired, now=datetime(2026, 11, 26, tzinfo=UTC)
    )


def test_resolve_check_cells_override_expires_at() -> None:
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    future = now + timedelta(hours=1)
    past = now - timedelta(seconds=1)

    add_remove_active = delivery.CheckRow(
        id="unit",
        argv=("pytest",),
        needs=("code",),
        cells=delivery._CellOverrides(
            add={
                "preview.post": delivery._OverrideReason(reason="temp preview", expires_at=future)
            },
            remove={"ci.pre": delivery._OverrideReason(reason="temp mute ci", expires_at=future)},
        ),
    )
    active_cells = delivery.resolve_check_cells(add_remove_active, now=now)
    assert "preview.post" in active_cells
    assert "ci.pre" not in active_cells

    off_active = delivery.CheckRow(
        id="unit",
        argv=("pytest",),
        needs=("code",),
        off=delivery._OverrideReason(reason="temp off", expires_at=future),
    )
    assert delivery.resolve_check_cells(off_active, now=now) == ()

    for expired_at in (past, now):
        add_remove_expired = delivery.CheckRow(
            id="unit",
            argv=("pytest",),
            needs=("code",),
            cells=delivery._CellOverrides(
                add={
                    "preview.post": delivery._OverrideReason(
                        reason="expired add", expires_at=expired_at
                    )
                },
                remove={
                    "ci.pre": delivery._OverrideReason(
                        reason="expired remove", expires_at=expired_at
                    )
                },
            ),
        )
        expired_cells = delivery.resolve_check_cells(
            add_remove_expired, now=now.replace(tzinfo=None)
        )
        assert "preview.post" not in expired_cells
        assert "ci.pre" in expired_cells

        off_expired = delivery.CheckRow(
            id="unit",
            argv=("pytest",),
            needs=("code",),
            off=delivery._OverrideReason(reason="expired off", expires_at=expired_at),
        )
        assert "ci.pre" in delivery.resolve_check_cells(off_expired, now=now)


def test_run_cell_enforces_cumulative_budget() -> None:
    budget = delivery.CELL_POLICIES["ci.pre"].budget_seconds
    manifest = delivery.RuntimeHooksManifest(
        schema_version=delivery._HOOKS_SCHEMA_VERSION,
        service="demo/budget",
        checks=(
            delivery.CheckRow(id="c1", argv=("echo", "1"), needs=("code",), timeout_seconds=150),
            delivery.CheckRow(id="c2", argv=("echo", "2"), needs=("code",), timeout_seconds=150),
            delivery.CheckRow(id="c3", argv=("echo", "3"), needs=("code",), timeout_seconds=150),
        ),
    )
    clock = [1000.0]
    seen_calls: list[tuple[list[str], float]] = []

    def _advancing_runner(argv, **kwargs):
        seen_calls.append((list(argv), float(kwargs["timeout"])))
        clock[0] += 140.0 if argv == ["echo", "1"] else 105.0
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    res = delivery._run_cell(
        manifest,
        "ci.pre",
        environ={},
        runner=_advancing_runner,
        monotonic=lambda: clock[0],
    )
    assert res.passed is False
    assert seen_calls == [
        (["echo", "1"], 150.0),
        (["echo", "2"], float(budget - 140)),
    ]
    assert [v.id for v in res.verdicts] == ["c1", "c2", "c3"]
    assert res.verdicts[0].passed is True and res.verdicts[1].passed is True
    assert res.verdicts[2].passed is False
    assert res.verdicts[2].exit_code == 124
    assert f"exhausted {budget}s budget" in res.verdicts[2].stderr

    boot_manifest = delivery.RuntimeHooksManifest(
        schema_version=delivery._HOOKS_SCHEMA_VERSION,
        service="demo/boot-budget",
        checks=(
            delivery.CheckRow(
                id="c1",
                argv=("echo", "main"),
                bootstrap="echo boot",
                needs=("code",),
                timeout_seconds=60,
            ),
        ),
    )
    boot_clock = [500.0]
    boot_calls: list[list[str]] = []

    def _boot_runner(argv, **_kwargs):
        boot_calls.append(list(argv))
        boot_clock[0] += float(budget) + 1.0
        return subprocess.CompletedProcess(argv, 0, stdout="boot ok", stderr="")

    boot_res = delivery._run_cell(
        boot_manifest,
        "ci.pre",
        environ={},
        runner=_boot_runner,
        monotonic=lambda: boot_clock[0],
    )
    assert boot_res.passed is False
    assert boot_calls == [["sh", "-c", "echo boot"]]
    assert boot_res.verdicts[0].exit_code == 124
    assert f"exhausted {budget}s budget" in boot_res.verdicts[0].stderr
