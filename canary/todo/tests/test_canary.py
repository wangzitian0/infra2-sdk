"""Lean end-to-end unit and contract tests for Canary Todo."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import canary_schema
import httpx
import worker
from canary_settings import CanarySettings
from conftest import TODO_DIR, load_todo_module, wire_fake_todo_pg
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.testclient import TestClient

from infra2_sdk.config import EnvironmentManifest
from infra2_sdk.delivery import RuntimeHooksManifest, resolve_check_cells
from infra2_sdk.service import DependencyStatus, ProbeResult


def test_canary_endpoints_probes_and_status_cache(monkeypatch) -> None:
    todo_mod = load_todo_module("todo_app_unit")
    store = wire_fake_todo_pg(monkeypatch)
    client = TestClient(todo_mod.fastapi_app)

    # Probes return HTTP 200 when gating dependencies pass, with first 128 bytes holding verdict.
    monkeypatch.setattr(
        todo_mod,
        "_check_postgres",
        lambda _u: {"status": "pass", "latency_ms": 1.0, "detail": "ok"},
    )
    monkeypatch.setattr(
        todo_mod, "_check_redis", lambda _u: {"status": "pass", "latency_ms": 1.0, "detail": "ok"}
    )
    monkeypatch.setattr(
        todo_mod, "_check_s3", lambda _c: {"status": "pass", "latency_ms": 1.0, "detail": "ok"}
    )
    seen_urls: list[str] = []

    async def _fake_probe_http(url: str, **_k: Any) -> ProbeResult:
        seen_urls.append(url)
        return ProbeResult(
            name="http", status=DependencyStatus.PRESENT, detail="HTTP 200", duration_ms=1.0
        )

    seen_retry_urls: list[str] = []

    async def _fake_retry(_client: Any, _method: str, url: str, **_k: Any) -> httpx.Response:
        seen_retry_urls.append(url)
        return httpx.Response(200, request=httpx.Request("GET", url))

    monkeypatch.setattr(todo_mod, "probe_http", _fake_probe_http)
    monkeypatch.setattr(todo_mod, "async_request_with_retry", _fake_retry)
    monkeypatch.setenv("AUTHENTIK_ENDPOINT", "https://auth.zitian.party/-/health/ready/")
    monkeypatch.setenv("OPENPANEL_API_URL", "https://openpanel.zitian.party/api")
    monkeypatch.setenv("OPENPANEL_HEALTH_URL", "http://platform-openpanel-api:3000/healthcheck")
    monkeypatch.setenv("OPENPANEL_CLIENT_ID", "cid-1")
    monkeypatch.setenv("OPENPANEL_SCRIPT_URL", "https://openpanel.zitian.party/op1.js")
    monkeypatch.setenv("NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT", "https://otel.zitian.party")
    monkeypatch.setenv("NEXT_PUBLIC_DEPLOYMENT_ENVIRONMENT", "staging")
    monkeypatch.setenv("NEXT_PUBLIC_GIT_SHA", "abc1234")
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv("CONFIGURATION_SHA256", "c" * 64)

    status_resp = client.get("/api/canary/status")
    assert status_resp.status_code == 200
    assert b'"ok":true' in status_resp.content[:128] or b'"ok": true' in status_resp.content[:128]
    body = status_resp.json()
    assert body["ok"] is True
    assert "http://platform-openpanel-api:3000/healthcheck" in seen_urls
    assert "https://openpanel.zitian.party/api" not in seen_urls
    assert seen_retry_urls == ["https://auth.zitian.party/-/health/ready/"]
    assert body["capabilities"] == {
        "openpanel_configured": True,
        "openpanel_api_url_set": True,
        "openpanel_script_url_set": True,
        "browser_otel_configured": True,
        "browser_deployment_environment": "staging",
        "browser_git_sha": "abc1234",
        "schema_enum": "canary_todo_capability",
    }

    assert client.get("/livez").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/health").status_code == 200
    api_health = client.get("/api/health", headers={"X-Request-ID": "req-99"})
    assert api_health.status_code == 200 and api_health.headers["X-Request-ID"] == "req-99"
    assert api_health.json()["canary"]["ok"] is True

    # Fail closed to 503 when Redis is unconfigured or failing.
    todo_mod._STATUS_CACHE = None
    monkeypatch.setattr(
        todo_mod, "_check_redis", lambda _u: {"status": "fail", "latency_ms": 1.0, "detail": "down"}
    )
    assert client.get("/api/canary/status").status_code == 503
    assert client.get("/readyz").status_code == 503

    # Full CRUD lifecycle on /api/todos + 400 validation + 404.
    assert client.post("/api/todos", json={"title": "   "}).status_code == 400
    created = client.post("/api/todos", json={"title": "verify sdk", "capability": "s3"})
    assert created.status_code == 201 and created.json()["capability"] == "s3"
    tid = created.json()["id"]
    assert len(client.get("/api/todos").json()) == 1 and store.table_created is True
    patched = client.patch(f"/api/todos/{tid}", json={"completed": True})
    assert patched.status_code == 200 and patched.json()["completed"] is True
    assert client.patch("/api/todos/9999", json={"completed": True}).status_code == 404
    deleted = client.delete(f"/api/todos/{tid}")
    assert deleted.status_code == 200 and deleted.json()["deleted_id"] == tid
    assert client.delete(f"/api/todos/{tid}").status_code == 404

    # Auth & escaped UI HTML badge.
    me = client.get(
        "/api/auth/me", headers={"X-authentik-username": "alice", "X-authentik-groups": "ops,dev"}
    )
    assert me.status_code == 200 and me.json() == {
        "authenticated": True,
        "username": "alice",
        "name": None,
        "email": None,
        "groups": ["ops", "dev"],
    }
    ui = client.get("/", headers={"X-authentik-username": "alice", "X-authentik-name": "<Alice>"})
    assert ui.status_code == 200 and "todo.zitian.party" in ui.text and "&lt;Alice&gt;" in ui.text


def test_canary_rate_limit_logging_settings_and_schema(monkeypatch) -> None:
    todo_mod = load_todo_module("todo_app_contracts")
    monkeypatch.setattr(todo_mod.SUMMARY_RATE_LIMIT, "acquire", lambda cost=1: (False, 3.0))
    client = TestClient(todo_mod.fastapi_app)
    resp = client.post("/api/todos/summary", json={"limit": 5})
    assert resp.status_code == 429 and resp.headers["Retry-After"] == "3"

    # Logging order: uvicorn.access suppresses probes while httpx logs stay filtered to WARNING+.
    assert logging.getLogger("httpx").level >= logging.WARNING
    assert logging.getLogger("httpcore").level >= logging.WARNING

    # Settings manifest & shared_env contract via validate_offline().
    manifest = EnvironmentManifest.from_model(CanarySettings, source="canary/todo")
    assert manifest.validate_offline() == []

    # Schema metadata exposes enum & all 5 columns of todos.
    assert "todos" in canary_schema.METADATA.tables
    assert len(canary_schema.METADATA.tables["todos"].columns) == 5
    cap_col = next(
        c for c in canary_schema.METADATA.tables["todos"].columns if c.name == "capability"
    )
    assert cap_col.type.name == canary_schema.CANARY_CAPABILITY_ENUM_NAME
    assert tuple(cap_col.type.enums) == canary_schema.CANARY_CAPABILITY_LABELS

    # Served routes contract.
    routes = {r.path for r in todo_mod.fastapi_app.routes}
    assert {
        "/",
        "/index.html",
        "/livez",
        "/readyz",
        "/health",
        "/api/health",
        "/api/canary/status",
        "/api/todos",
        "/api/todos/{todo_id}",
        "/api/todos/summary",
        "/api/auth/me",
    } <= routes


def test_canary_worker_and_hooks(monkeypatch, capsys) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test-worker")

    responses = [
        httpx.Response(
            503,
            json={"status": "not_ready"},
            request=httpx.Request("GET", "http://127.0.0.1:8000/readyz"),
        ),
        httpx.Response(
            200,
            json={"status": "ready", "service": "platform-todo"},
            request=httpx.Request("GET", "http://127.0.0.1:8000/readyz"),
        ),
    ]

    class _Client:
        def request(self, method: str, url: str, **_k: Any) -> httpx.Response:
            return responses.pop(0)

    out = worker.run_once(
        health_url="http://127.0.0.1:8000/readyz",
        client=_Client(),
        tracer=tracer,
        sleep=lambda _: None,
    )
    assert out["ok"] is True and out["service"] == "platform-todo"
    spans = exporter.get_finished_spans()
    assert len(spans) == 1 and spans[0].attributes["canary.worker.ok"] is True

    monkeypatch.setattr(
        worker, "run_once", lambda *, health_url: {"ok": True, "health_url": health_url}
    )
    assert worker.main(["--once", "--health-url", "http://127.0.0.1:8000/readyz"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    hooks = RuntimeHooksManifest.from_file(Path(TODO_DIR) / "runtime-hooks.manifest.json")
    assert set(resolve_check_cells(hooks.checks[0])) >= {
        "preview.operation",
        "staging.operation",
        "prod.operation",
    }
    import subprocess
    import sys

    cli_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "infra2_sdk.delivery",
            "check-hooks",
            str(Path(TODO_DIR) / "runtime-hooks.manifest.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert cli_proc.returncode == 0
    assert "staging.operation" in json.loads(cli_proc.stdout)["checks"]["smoke"]
