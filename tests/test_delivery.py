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
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stages = delivery.load_stages()
    assert stages["policy_version"] == delivery.POLICY_VERSION
    assert "staging.operation" in delivery.CELL_POLICIES

    smoke_row = delivery.CheckRow(
        id="smoke", argv=("echo", "ok"), needs=("code", "running_service"), writes=False
    )
    assert delivery.resolve_check_cells(smoke_row) == (
        "preview.operation",
        "staging.operation",
        "prod.operation",
    )

    # Full Schema v3 compatibility (cells.add/remove, bootstrap, issue, off, artifacts_dir).
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
    assert "prod.post" not in rw_cells  # writes=True never allowed in prod
    assert (
        "staging.post" in rw_cells and "scheduled_audit" in rw_cells
    )  # accepts_scale=False included in scheduled_audit

    # Expired cells.add override is ignored so code-only check stays in code-only cells.
    assert set(delivery.resolve_check_cells(v3_manifest.checks[1], now=now)) == {
        "local.pre",
        "local.post",
        "ci.pre",
        "ci.post",
    }

    # Explicitly adding writes=True to prod.post must fail closed.
    bad_prod = delivery.CheckRow(
        id="unsafe",
        argv=("echo", "x"),
        needs=("code", "running_service"),
        writes=True,
        cells=delivery.CellOverrides(add={"prod.post": delivery.OverrideReason(reason="unsafe")}),
    )
    with pytest.raises(ValueError, match="cannot run in prod"):
        delivery.resolve_check_cells(bad_prod)

    # Active `off` check skips execution -> fail closed when 0 active checks remain.
    muted_only = delivery.RuntimeHooksManifest(
        schema_version=3,
        service="demo/muted",
        checks=(v3_manifest.checks[2],),
    )
    assert delivery.run_cell(muted_only, "staging.post", now=now).passed is False

    manifest = delivery.RuntimeHooksManifest(
        schema_version=delivery.HOOKS_SCHEMA_VERSION,
        service="demo/svc",
        required_env=("APP_URL",),
        optional_env=("TARGET_URL",),
        checks=(smoke_row,),
    )
    env = delivery.build_hook_env(
        manifest,
        {"APP_URL": "http://127.0.0.1:8000", "DATABASE_URL": "postgresql://secret", "AWS_KEY": "x"},
        cell="staging.operation",
    )
    assert env["APP_URL"] == "http://127.0.0.1:8000"
    assert (
        env["HOOK_CELL"] == "staging.operation" and env["HOOK_ARTIFACTS_DIR"] == ".artifacts/hooks"
    )
    assert "DATABASE_URL" not in env and "AWS_KEY" not in env

    res = delivery.run_cell(
        manifest,
        "staging.operation",
        environ={"APP_URL": "http://127.0.0.1:8000"},
        runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="ok", stderr=""),
    )
    assert res.passed is True and len(res.verdicts) == 1
    out_file = delivery.record_verdicts(res.verdicts, tmp_path / "verdicts.json")
    assert json.loads(out_file.read_text(encoding="utf-8"))[0]["passed"] is True

    m_path = tmp_path / "hooks.json"
    m_path.write_text(manifest.to_json(), encoding="utf-8")
    assert delivery.main(["check-hooks", str(m_path)]) == 0
    monkeypatch.setenv("APP_URL", "http://127.0.0.1:8000")
    assert (
        delivery.main(
            [
                "run-cell",
                str(m_path),
                "--cell",
                "staging.operation",
                "--verdicts-out",
                str(tmp_path / "c.json"),
            ]
        )
        == 0
    )

    # TimeoutExpired bytes stdout & stderr are decoded cleanly; OSError records exit_code 127.
    def _timeout_runner(argv, **_kw):
        raise subprocess.TimeoutExpired(argv, 1, output=b"partial-out", stderr=b"partial-err")

    tout = delivery.run_cell(
        manifest,
        "staging.operation",
        environ={"APP_URL": "http://127.0.0.1:8000"},
        runner=_timeout_runner,
    )
    assert tout.passed is False and tout.verdicts[0].exit_code == 124
    assert tout.verdicts[0].stdout == "partial-out" and "partial-err" in tout.verdicts[0].stderr

    os_err = delivery.run_cell(
        manifest,
        "staging.operation",
        environ={"APP_URL": "http://127.0.0.1:8000"},
        runner=lambda _a, **_k: (_ for _ in ()).throw(FileNotFoundError("no bin")),
    )
    assert os_err.passed is False and os_err.verdicts[0].exit_code == 127


def test_deploy_contracts_and_health_polling() -> None:
    req_at = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="later than"):
        delivery.DeployRequest(
            operation=delivery.DeployOperation.DEPLOY,
            deploy_type=delivery.DeployType.STAGING,
            target_ref="v4.11.0",
            health_url="https://todo.zitian.party/api/health",
            requested_at=req_at,
            accept_until=req_at - timedelta(seconds=1),
        )

    # Rolling deploy waits past >5 mismatched 200 responses until git_sha prefix converges.
    rolling_responses = [
        *[(200, b'{"version":"old-tag","git_sha":"0000000000000000000000000000000000000000"}')] * 7,
        (200, b'{"version":"v4.11.0","git_sha":"abcdef1234567890"}'),
    ]
    now = [0.0]

    res = delivery.poll_until_healthy(
        "https://todo.zitian.party/api/health",
        expected_version="abcdef1",
        timeout_seconds=30.0,
        interval_seconds=1.0,
        http_get=lambda _u, _t: rolling_responses.pop(0),
        monotonic=lambda: now[0],
        sleep=lambda s: now.__setitem__(0, now[0] + s),
    )
    assert res.ok is True and res.attempts == 8

    # Redirects are not followed by default (no SSO login redirect false 200).
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/redir":
                self.send_response(302)
                self.send_header("Location", "/ok")
                self.end_headers()
                return
            body = b"plain-ok" if self.path == "/text" else b'{"service_version":"v4.11.0"}'
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert delivery.default_http_get(f"http://127.0.0.1:{srv.server_port}/redir", 2.0)[0] == 302
        assert (
            delivery.main(
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
            delivery.main(
                [
                    "poll",
                    "--url",
                    f"http://127.0.0.1:{srv.server_port}/ok",
                    "--expected-version",
                    "v4.11.0",
                    "--timeout-seconds",
                    "2",
                ]
            )
            == 0
        )
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

    ci_res = delivery.run_cell(
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
        schema_version=delivery.HOOKS_SCHEMA_VERSION,
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
    ok_res = delivery.run_cell(manifest, "staging.operation", environ={}, runner=ok_runner)
    assert ok_res.passed is True
    assert seen_commands == [["sh", "-c", "echo prep"], ["echo", "run"]]

    seen_commands.clear()

    def failing_bootstrap_runner(argv, **_kwargs):
        seen_commands.append(list(argv))
        return subprocess.CompletedProcess(argv, 2, stdout="", stderr="pip install failed")

    fail_res = delivery.run_cell(
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

    timeout_res = delivery.run_cell(
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
        off=delivery.OverrideReason(reason="paused", issue="wangzitian0/infra2#1823"),
    )
    assert delivery.resolve_check_cells(check_off) == ()

    expired = delivery.CheckRow(
        id="staging_gate",
        argv=("echo", "gate"),
        needs=("code", "running_service"),
        off=delivery.OverrideReason(
            reason="paused",
            expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        ),
    )
    assert "staging.post" in delivery.resolve_check_cells(
        expired, now=datetime(2026, 11, 26, tzinfo=UTC)
    )
