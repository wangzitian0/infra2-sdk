"""Tests for Canary Todo App-peer contracts (worker, schema_check, hooks, capabilities)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import canary_schema
import httpx
import worker
from conftest import TODO_DIR, load_todo_module
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.testclient import TestClient

from infra2_sdk.config import EnvironmentManifest, check_shared_env
from infra2_sdk.delivery import RuntimeHooksManifest, resolve_check_cells
from infra2_sdk.storage import (
    ROLLBACK_CLASS_A,
    ROLLBACK_CLASS_C,
    EnumSchemaSpec,
    check_enums,
    classify_rollback,
    enums_from_metadata,
)


class _SequenceClient:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append((method, url))
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _fake_response(status_code: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=payload,
        request=httpx.Request("GET", "http://127.0.0.1:8000/api/health"),
    )


def test_canary_schema_enums_and_rollback_classes(monkeypatch) -> None:
    monkeypatch.syspath_prepend(str(TODO_DIR))
    code_enums = enums_from_metadata(canary_schema.METADATA)
    name = canary_schema.CANARY_CAPABILITY_ENUM_NAME
    labels = canary_schema.CANARY_CAPABILITY_LABELS
    assert code_enums == (EnumSchemaSpec(schema_name="public", name=name, labels=labels),)

    exact = check_enums(code_enums, {("public", name): labels})
    assert exact == ()
    assert classify_rollback(enum_discrepancies=exact) == ROLLBACK_CLASS_A

    additive = check_enums(code_enums, {("public", name): labels[:-1]})
    assert len(additive) == 1 and additive[0].kind == "additive_labels"
    assert additive[0].missing_in_db == (labels[-1],)
    assert classify_rollback(enum_discrepancies=additive) == ROLLBACK_CLASS_A

    extra_in_db = check_enums(code_enums, {("public", name): (*labels, "legacy_extra")})
    assert len(extra_in_db) == 1 and extra_in_db[0].kind == "destructive_labels"
    assert extra_in_db[0].missing_in_code == ("legacy_extra",)
    assert classify_rollback(enum_discrepancies=extra_in_db) == ROLLBACK_CLASS_C

    casing_drift = check_enums(code_enums, {("public", name): (labels[0].upper(), *labels[1:])})
    assert len(casing_drift) == 1 and casing_drift[0].kind == "destructive_labels"
    assert classify_rollback(enum_discrepancies=casing_drift) == ROLLBACK_CLASS_C


def test_worker_run_once_retries_and_records_otel_span() -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test-worker")

    client = _SequenceClient(
        [
            _fake_response(503, {"status": "not_ready"}),
            _fake_response(200, {"status": "ready", "service": "platform-todo"}),
        ]
    )
    result = worker.run_once(
        health_url="http://127.0.0.1:8000/readyz",
        client=client,
        tracer=tracer,
        sleep=lambda _: None,
    )
    assert result["ok"] is True
    assert result["status_code"] == 200
    assert result["service"] == "platform-todo"
    assert len(client.calls) == 2

    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "canary.worker.tick"
    assert spans[0].attributes["canary.worker.ok"] is True
    assert spans[0].attributes["canary.worker.status_code"] == 200


def test_worker_run_once_reports_persistent_error_and_exception() -> None:
    bad_status_client = _SequenceClient([_fake_response(503, {"ok": False})])
    bad_res = worker.run_once(
        health_url="http://127.0.0.1:8000/api/health",
        max_attempts=1,
        client=bad_status_client,
        sleep=lambda _: None,
    )
    assert bad_res["ok"] is False
    assert bad_res["status_code"] == 503

    err_client = _SequenceClient([httpx.ConnectError("refused")])
    err_res = worker.run_once(
        health_url="http://127.0.0.1:8000/api/health",
        max_attempts=1,
        client=err_client,
        sleep=lambda _: None,
    )
    assert err_res["ok"] is False
    assert err_res["status_code"] == 0
    assert "ConnectError" in err_res["detail"]


def test_worker_main_once_calls_init_worker(monkeypatch, capsys) -> None:
    seen: dict[str, Any] = {}

    class _FakeProviders:
        def __init__(self) -> None:
            self.shut_down = False

        def shutdown(self) -> None:
            self.shut_down = True

    providers = _FakeProviders()

    def _fake_init_worker(
        name: str, *, strict: bool = True, require_issued_identity: bool = False
    ) -> _FakeProviders:
        seen["name"] = name
        seen["strict"] = strict
        seen["require_issued_identity"] = require_issued_identity
        return providers

    monkeypatch.setattr(worker, "init_worker", _fake_init_worker)
    monkeypatch.setattr(
        worker,
        "run_once",
        lambda *, health_url: {
            "ok": True,
            "status_code": 200,
            "service": "platform/todo",
            "health_url": health_url,
            "detail": "ok",
        },
    )
    rc = worker.main(["--once", "--health-url", "http://127.0.0.1:8000/api/health"])
    assert rc == 0
    assert seen == {
        "name": "platform-todo-worker",
        "strict": True,
        "require_issued_identity": True,
    }
    assert providers.shut_down is True
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_worker_main_once_runs_standalone_without_environment_var(monkeypatch, capsys) -> None:
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr(
        worker,
        "run_once",
        lambda *, health_url: {
            "ok": True,
            "status_code": 200,
            "service": "platform/todo",
            "health_url": health_url,
            "detail": "ok",
        },
    )
    assert worker.main(["--once", "--health-url", "http://127.0.0.1:8000/api/health"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_canary_hooks_manifest_and_run_cell(monkeypatch) -> None:
    from subprocess import CompletedProcess

    from infra2_sdk.delivery import run_cell

    manifest = RuntimeHooksManifest.from_file(Path(TODO_DIR) / "runtime-hooks.manifest.json")
    assert manifest.schema_version == 3
    assert manifest.service == "canary/todo"
    assert [c.id for c in manifest.checks] == ["smoke"]
    assert "/readyz" in manifest.checks[0].argv[-1]
    cells = resolve_check_cells(manifest.checks[0])
    assert {"preview.operation", "staging.operation", "prod.operation"} <= set(cells)

    seen: list[list[str]] = []

    def _fake_runner(argv, **_kwargs):
        seen.append(list(argv))
        return CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

    res = run_cell(
        manifest,
        "staging.operation",
        environ={"APP_URL": "http://127.0.0.1:8000", "DATABASE_URL": "secret://hidden"},
        runner=_fake_runner,
    )
    assert res.passed is True
    assert len(seen) == 1


def test_canary_summary_rate_limit(monkeypatch) -> None:
    todo_mod = load_todo_module("todo_app_rate_limit")
    monkeypatch.setattr(todo_mod.SUMMARY_RATE_LIMIT, "acquire", lambda cost=1: (False, 3.0))
    client = TestClient(todo_mod.fastapi_app)
    resp = client.post("/api/todos/summary", json={"limit": 5})
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "3"
    assert resp.json()["detail"] == "rate_limited"


def test_canary_env_manifest_conforms_to_shared_env_and_markdown() -> None:
    manifest = EnvironmentManifest.from_dict(
        json.loads((Path(TODO_DIR) / "env.manifest.json").read_text(encoding="utf-8"))
    )
    violations = check_shared_env(f.env for f in manifest.fields)
    assert violations == []
    markdown = (Path(TODO_DIR) / "env-reference.generated.md").read_text(encoding="utf-8")
    assert markdown == manifest.render_markdown()


def test_openpanel_health_url_fallback_and_capability_summary(monkeypatch) -> None:
    todo_mod = load_todo_module("todo_app_capabilities")
    seen_urls: list[str] = []
    ok = {"status": "pass", "latency_ms": 1.0, "detail": "ok"}

    monkeypatch.setattr(todo_mod, "_probe_postgres", lambda *a, **k: dict(ok))
    monkeypatch.setattr(todo_mod, "_probe_redis", lambda *a, **k: dict(ok))
    monkeypatch.setattr(todo_mod, "_probe_s3", lambda *a, **k: dict(ok))
    monkeypatch.setattr(
        todo_mod,
        "_probe_http",
        lambda url, **k: seen_urls.append(url) or dict(ok),
    )

    monkeypatch.setenv("DATABASE_URL", "postgresql://u:pppppppppppppppp@h/d")
    monkeypatch.setenv("OPENPANEL_API_URL", "https://openpanel.zitian.party/api")
    monkeypatch.setenv("OPENPANEL_HEALTH_URL", "http://platform-openpanel-api:3000/healthcheck")
    monkeypatch.setenv("OPENPANEL_CLIENT_ID", "client-123")
    monkeypatch.setenv("OPENPANEL_SCRIPT_URL", "https://openpanel.zitian.party/op1.js")
    monkeypatch.setenv("NEXT_PUBLIC_OTEL_EXPORTER_OTLP_ENDPOINT", "https://otel.zitian.party")
    monkeypatch.setenv("NEXT_PUBLIC_DEPLOYMENT_ENVIRONMENT", "staging")
    monkeypatch.setenv("NEXT_PUBLIC_GIT_SHA", "abc1234")

    client = TestClient(todo_mod.fastapi_app)
    resp = client.get("/api/canary/status")
    assert resp.status_code == 200
    body = resp.json()
    assert "http://platform-openpanel-api:3000/healthcheck" in seen_urls
    assert "https://openpanel.zitian.party/api" not in seen_urls
    assert body["capabilities"] == {
        "openpanel_configured": True,
        "openpanel_api_url_set": True,
        "openpanel_script_url_set": True,
        "browser_otel_configured": True,
        "browser_deployment_environment": "staging",
        "browser_git_sha": "abc1234",
        "schema_enum": "canary_todo_capability",
    }
