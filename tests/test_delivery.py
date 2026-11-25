"""Tests for infra2_sdk.delivery."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
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
    assert stages["bootstrap_cap_seconds"] == delivery.BOOTSTRAP_CAP_SECONDS
    assert "staging.operation" in delivery.CELL_POLICIES

    smoke_row = delivery.CheckRow(
        id="smoke", argv=("echo", "ok"), needs=("code", "running_service"), writes=False
    )
    assert delivery.resolve_check_cells(smoke_row) == (
        "preview.operation",
        "staging.operation",
        "prod.operation",
    )

    rw_row = delivery.CheckRow(
        id="e2e-write",
        argv=("echo", "write"),
        needs=("code", "running_service"),
        writes=True,
        accepts_scale=True,
        overrides={
            "preview.post": delivery.OverrideReason(action="exclude", reason="skip preview write"),
            "prod.post": delivery.OverrideReason(
                action="include", reason="approved read-write check"
            ),
        },
    )
    cells = delivery.resolve_check_cells(rw_row)
    assert "preview.post" not in cells
    assert "prod.post" in cells and "staging.post" in cells and "scheduled_audit" in cells

    manifest = delivery.RuntimeHooksManifest(
        schema_version=delivery.HOOKS_SCHEMA_VERSION,
        service="demo/svc",
        required_env=("APP_URL",),
        optional_env=("TARGET_URL",),
        checks=(smoke_row,),
    )
    env = delivery.build_hook_env(
        manifest,
        {
            "APP_URL": "http://127.0.0.1:8000",
            "DATABASE_URL": "postgresql://secret",
            "SECRET_FOO": "x",
        },
    )
    assert env == {"APP_URL": "http://127.0.0.1:8000"}
    assert "DATABASE_URL" not in env and "SECRET_FOO" not in env

    with pytest.raises(ValueError, match="forbidden sensitive env var"):
        delivery.RuntimeHooksManifest(
            service="demo",
            required_env=("DATABASE_URL",),
            checks=(smoke_row,),
        )

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
    echo_manifest = delivery.RuntimeHooksManifest(
        schema_version=delivery.HOOKS_SCHEMA_VERSION,
        service="demo/svc",
        required_env=("APP_URL",),
        checks=(
            delivery.CheckRow(
                id="smoke",
                argv=("python3", "-c", "print('ok')"),
                needs=("code", "running_service"),
                writes=False,
            ),
        ),
    )
    e_path = tmp_path / "echo-hooks.json"
    e_path.write_text(echo_manifest.to_json(), encoding="utf-8")
    verdict_out = tmp_path / "cli.json"
    assert (
        delivery.main(
            [
                "run-cell",
                str(e_path),
                "--cell",
                "staging.operation",
                "--verdicts-out",
                str(verdict_out),
            ]
        )
        == 0
    )

    def _timeout_runner(argv, **_kw):
        raise subprocess.TimeoutExpired(argv, 1, output="partial")

    tout = delivery.run_cell(
        manifest,
        "staging.operation",
        environ={"APP_URL": "http://127.0.0.1:8000"},
        runner=_timeout_runner,
    )
    assert tout.passed is False and tout.verdicts[0].exit_code == 124


def test_deploy_contracts_and_health_polling() -> None:
    req_at = datetime(2026, 11, 25, 10, 0, tzinfo=UTC)
    until = delivery.production_request_accept_until(req_at, ttl_seconds=900)
    assert until > req_at
    assert delivery.parse_sender_deadline("2026-11-25T10:15:00Z") == until

    req = delivery.DeployRequest(
        operation=delivery.DeployOperation.DEPLOY,
        deploy_type=delivery.DeployType.STAGING,
        target_ref="v4.10.0",
        health_url="https://todo.zitian.party/api/health",
        requested_at=req_at,
        accept_until=until,
    )
    assert req.deploy_type is delivery.DeployType.STAGING
    ev = delivery.DeployEvidence(
        commit_sha="a" * 40, release_tag="v4.10.0", ci_run_id="123", recorded_at=req_at
    )
    assert ev.release_tag == "v4.10.0"

    responses = [
        (503, b'{"status":"starting"}'),
        (200, b'{"service_version":"old"}'),
        (200, b'{"service_version":"v4.10.0"}'),
    ]
    now = [0.0]

    def _get(_url: str, _timeout: float) -> tuple[int, bytes]:
        return responses.pop(0)

    def _sleep(sec: float) -> None:
        now[0] += sec

    res = delivery.poll_until_healthy(
        "https://todo.zitian.party/api/health",
        expected_version="v4.10.0",
        timeout_seconds=30.0,
        interval_seconds=1.0,
        http_get=_get,
        monotonic=lambda: now[0],
        sleep=_sleep,
    )
    assert res.ok is True and res.attempts == 3 and res.observed_version == "v4.10.0"

    mismatch = delivery.poll_until_healthy(
        "https://todo.zitian.party/api/health",
        expected_version="v4.10.0",
        max_version_mismatch_attempts=2,
        http_get=lambda _u, _t: (200, b'{"service_version":"stale"}'),
        monotonic=lambda: 0.0,
        sleep=lambda _s: None,
    )
    assert mismatch.ok is False and mismatch.attempts == 2

    timed_out = delivery.poll_until_healthy(
        "https://todo.zitian.party/api/health",
        timeout_seconds=1.0,
        interval_seconds=0.5,
        http_get=lambda _u, _t: (_ for _ in ()).throw(OSError("down")),
        monotonic=lambda: now.__setitem__(0, now[0] + 0.6) or now[0],
        sleep=lambda _s: None,
    )
    assert timed_out.ok is False and "OSError" in timed_out.detail

    class _HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = b'{"service_version":"v4.10.0"}' if self.path == "/ok" else b"{}"
            self.send_response(200 if self.path == "/ok" else 503)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert delivery.default_http_get(f"http://127.0.0.1:{srv.server_port}/down", 2.0)[0] == 503
        assert (
            delivery.main(
                [
                    "poll",
                    "--url",
                    f"http://127.0.0.1:{srv.server_port}/ok",
                    "--expected-version",
                    "v4.10.0",
                    "--timeout-seconds",
                    "2",
                ]
            )
            == 0
        )
    finally:
        srv.shutdown()
        srv.server_close()
