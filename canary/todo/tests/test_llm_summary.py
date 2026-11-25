"""Offline and VCR regression tests for Canary Todo LLM summary."""

from __future__ import annotations

import io
import json
import logging
import os
import sys
import threading
import warnings
from contextlib import contextmanager
from datetime import UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
from conftest import (
    FakeTodoPgStore,
    clear_telemetry_env,
    load_todo_module,
    wire_fake_todo_pg,
)
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic_ai import UsageLimits, models
from pydantic_ai.capabilities import RetryOnModelErrors
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage
from starlette.testclient import TestClient

from infra2_sdk.llm import (
    LlmSettings,
    allow_vcr_model_requests,
    create_llm_agent,
    redact_llm_error,
    vcr_llm_config,
)
from infra2_sdk.telemetry import OtelSettings, configure_telemetry

models.ALLOW_MODEL_REQUESTS = False

CASSETTE_DIR = Path(__file__).resolve().parent / "cassettes" / "todo_llm_summary"
_AMBIENT_RECORD_API_KEY = os.environ.get("LLM_API_KEY", "")

SAMPLE_TODOS: list[dict[str, Any]] = [
    {
        "id": 1,
        "title": "SECRET_TODO_ALPHA_991 Check Postgres replication lag",
        "completed": False,
        "capability": "postgres",
        "created_at": "2026-11-20T05:00:00Z",
    },
    {
        "id": 2,
        "title": "Verify Redis key TTL probe",
        "completed": True,
        "capability": "redis",
        "created_at": "2026-11-20T05:01:00Z",
    },
]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    clear_telemetry_env(monkeypatch)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_SMALL_MODEL", raising=False)
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://todo_app:test_todo_pg_pass_1234@platform-postgres:5432/postgres",
    )


@pytest.fixture(scope="module")
def vcr_config() -> dict[str, Any]:
    return vcr_llm_config(cassette_library_dir=str(CASSETTE_DIR))


def _load_todo_app() -> Any:
    return load_todo_module("todo_app_llm_summary")


def _sample_summary_json() -> str:
    return json.dumps(
        {
            "headline": "One open Postgres check, one completed Redis check.",
            "open_count": 1,
            "completed_count": 1,
            "focus": "Check Postgres replication lag",
        }
    )


def _seed_store(store: FakeTodoPgStore) -> None:
    from datetime import datetime

    store.table_created = True
    for row in SAMPLE_TODOS:
        store.next_id += 1
        store.rows.append(
            {
                "id": row["id"],
                "title": row["title"],
                "completed": row["completed"],
                "capability": row["capability"],
                "created_at": datetime(2026, 11, 20, 5, 0, tzinfo=UTC),
            }
        )


def _metric_attributes_by_name(reader: InMemoryMetricReader, name: str) -> list[dict[str, Any]]:
    data = reader.get_metrics_data()
    out: list[dict[str, Any]] = []
    for rm in data.resource_metrics:
        for sm in rm.scope_metrics:
            for metric in sm.metrics:
                if metric.name == name:
                    for dp in metric.data.data_points:
                        out.append(dict(dp.attributes))
    return out


class _Ok200Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length > 0:
            self.rfile.read(length)
        self.send_response(200)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _load_todo_app_with_sdk_formatter() -> tuple[Any, logging.Formatter]:
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    root.handlers = []
    try:
        todo_mod = _load_todo_app()
        stream_handlers = [h for h in root.handlers if isinstance(h, logging.StreamHandler)]
        assert stream_handlers and stream_handlers[0].formatter is not None
        return todo_mod, stream_handlers[0].formatter
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)


@contextmanager
def _capture_otel_and_logs(logger: logging.Logger, sdk_formatter: logging.Formatter):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Ok200Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()

    providers = configure_telemetry(
        OtelSettings(
            service_name="platform-todo",
            endpoint=f"http://127.0.0.1:{srv.server_port}",
            environment="staging",
            deployment_environment="staging",
            resource_attributes={},
        ),
        set_global=False,
        capture_logs=False,
    )
    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))

    metric_reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[metric_reader])

    log_exporter = InMemoryLogRecordExporter()
    assert providers.logger_provider is not None
    providers.logger_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from opentelemetry.sdk._logs import LoggingHandler

        otlp_handler = LoggingHandler(level=logging.INFO, logger_provider=providers.logger_provider)

    stream_buf = io.StringIO()
    json_handler = logging.StreamHandler(stream_buf)
    json_handler.setLevel(logging.INFO)
    json_handler.setFormatter(sdk_formatter)

    prev_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(otlp_handler)
    logger.addHandler(json_handler)
    try:
        yield (
            tracer_provider,
            meter_provider,
            span_exporter,
            metric_reader,
            log_exporter,
            stream_buf,
        )
    finally:
        logger.removeHandler(otlp_handler)
        logger.removeHandler(json_handler)
        logger.setLevel(prev_level)
        tracer_provider.shutdown()
        meter_provider.shutdown()
        providers.shutdown()
        srv.shutdown()
        srv.server_close()


def test_summary_unconfigured_without_api_key_and_status_excludes_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert models.ALLOW_MODEL_REQUESTS is False
    todo_mod = _load_todo_app()
    assert "llm" not in todo_mod.GATING_CHECKS
    assert "llm" not in todo_mod.INFORMATIONAL_CHECKS

    store = wire_fake_todo_pg(monkeypatch)
    _seed_store(store)
    client = TestClient(todo_mod.fastapi_app)
    resp = client.post("/api/todos/summary", json={"limit": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unconfigured"
    assert body["summary"] is None
    assert body["detail"] == "Missing LLM_API_KEY"
    assert body["decoding_version"] == "todo.summary.v1"


@pytest.mark.parametrize("stream_flag", [False, True])
async def test_summary_otel_spans_metrics_logs_and_redaction(
    monkeypatch: pytest.MonkeyPatch,
    stream_flag: bool,
) -> None:
    assert "structlog" not in sys.modules
    monkeypatch.setenv("LLM_API_KEY", "test-zai-key")
    todo_mod, sdk_formatter = _load_todo_app_with_sdk_formatter()

    fake_model = TestModel(custom_output_args=json.loads(_sample_summary_json()))

    with _capture_otel_and_logs(todo_mod.LOGGER, sdk_formatter) as (
        tracer_provider,
        meter_provider,
        span_exporter,
        metric_reader,
        log_exporter,
        stream_buf,
    ):
        res = await todo_mod.summarize_recent_todos(
            limit=5,
            stream=stream_flag,
            model_override=fake_model,
            todos_loader=lambda _n: SAMPLE_TODOS,
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
        )

    assert res.status == "pass"
    assert res.todo_count == 2
    assert res.decoding_version == "todo.summary.v1"
    assert res.summary is not None
    assert res.summary.open_count == 1
    assert res.summary.completed_count == 1
    assert len(res.prompt_sha256 or "") == 64
    assert len(res.request_sha256 or "") == 64

    spans = span_exporter.get_finished_spans()
    span_names = [s.name for s in spans]
    assert f"invoke_agent {todo_mod.TODO_SUMMARY_CALL_SITE}" in span_names
    chat_spans = [s for s in spans if s.name.startswith("chat ")]
    assert len(chat_spans) >= 1

    # Dotted OTel attributes must align across spans, metrics, JSON logs, and OTLP LogRecords.
    chat_attrs = dict(chat_spans[0].attributes or {})
    assert chat_attrs["infra.llm.call_site"] == "todo.summary"
    assert chat_attrs["infra.llm.decoding_version"] == "todo.summary.v1"
    assert chat_attrs["infra.llm.prompt_sha256"] == res.prompt_sha256
    assert str(chat_attrs["code.file.path"]).endswith("todo/app.py")
    assert chat_attrs["code.function.name"] == "summarize_recent_todos"
    assert int(chat_attrs["code.line.number"]) > 0

    # Privacy/redaction: include_content=False & include_model_request_parameters=False.
    serialized_spans = json.dumps([dict(s.attributes or {}) for s in spans], default=str)
    assert "SECRET_TODO_ALPHA_991" not in serialized_spans
    assert "One open Postgres check" not in serialized_spans
    assert "Summarize recent infrastructure todo items" not in serialized_spans
    assert "model_request_parameters" not in serialized_spans
    assert "gen_ai.system_instructions" not in serialized_spans
    assert "final_result" not in chat_attrs
    assert '"content":' not in serialized_spans
    assert '"arguments":' not in serialized_spans
    input_msgs = json.loads(str(chat_attrs.get("gen_ai.input.messages", "[]")))
    assert len(input_msgs) >= 1
    assert all("content" not in part for msg in input_msgs for part in msg.get("parts", []))

    # Metrics match dotted infra.llm.* keys.
    dur_attrs = _metric_attributes_by_name(metric_reader, "gen_ai.client.operation.duration")
    assert len(dur_attrs) >= 1
    assert any(
        a.get("infra.llm.call_site") == "todo.summary"
        and a.get("infra.llm.decoding_version") == "todo.summary.v1"
        for a in dur_attrs
    )

    # Structlog-free stdout JSON log and OTLP LogRecord match dotted keys.
    json_lines = [json.loads(line) for line in stream_buf.getvalue().splitlines() if line.strip()]
    summary_logs = [entry for entry in json_lines if "todo summary ok" in entry["message"]]
    assert len(summary_logs) == 1
    log_entry = summary_logs[0]

    emitted_logs = log_exporter.get_finished_logs()
    otlp_summary = [
        item.log_record
        for item in emitted_logs
        if "todo summary ok" in str(getattr(item.log_record, "body", ""))
    ]
    assert len(otlp_summary) == 1
    otlp_attrs = dict(otlp_summary[0].attributes or {})

    for key in (
        "infra.llm.call_site",
        "infra.llm.decoding_version",
        "infra.llm.prompt_sha256",
        "code.file.path",
        "code.function.name",
    ):
        assert log_entry[key] == chat_attrs[key]
        assert otlp_attrs[key] == chat_attrs[key]
    assert int(log_entry["code.line.number"]) > 0
    assert int(otlp_attrs["code.line.number"]) == int(log_entry["code.line.number"])


def test_agent_missing_call_site_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = LlmSettings(provider="zai", api_key="test-key", small_model="glm-5.3-flash")
    agent = create_llm_agent(settings, model=TestModel(custom_output_text="ok"))
    with caplog.at_level(logging.WARNING, logger="infra2_sdk.llm"):
        res = agent.run_sync("ping")
    assert res.output == "ok"
    assert any("missing explicit call_site" in rec.message for rec in caplog.records)
    assert any(
        "test_llm_summary.test_agent_missing_call_site_warns" in rec.message
        for rec in caplog.records
    )


def test_zai_agent_disables_openai_sdk_retries_and_wires_retry_capability() -> None:
    settings = LlmSettings(
        provider="zai",
        api_key="test-key",
        base_url="https://api.z.ai/api/coding/paas/v4",
        small_model="glm-5.3-flash",
    )
    agent = create_llm_agent(settings, call_site="todo.summary", max_attempts=3)
    raw_zai = agent.model.wrapped
    assert raw_zai.client.max_retries == 0
    retry_caps = [
        c for c in agent._root_capability.capabilities if isinstance(c, RetryOnModelErrors)
    ]
    assert len(retry_caps) == 1
    assert retry_caps[0].max_attempts == 3


async def test_usage_limit_exceeded_propagates_without_retry_or_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    calls: list[str] = []

    def _primary(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        calls.append("primary")
        return ModelResponse(
            parts=[TextPart(_sample_summary_json())],
            usage=RequestUsage(input_tokens=50, output_tokens=500),
            model_name="glm-5.3-flash",
        )

    def _fallback(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        calls.append("fallback")
        return ModelResponse(parts=[TextPart(_sample_summary_json())])

    settings = LlmSettings(provider="zai", api_key="test-key", small_model="glm-5.3-flash")
    agent = create_llm_agent(
        settings,
        model=FunctionModel(_primary),
        fallback_models=[FunctionModel(_fallback)],
        call_site="todo.summary",
        max_attempts=3,
        usage_limits=UsageLimits(request_limit=1, output_tokens_limit=10),
    )
    with pytest.raises(UsageLimitExceeded):
        await agent.run("summarize")
    assert calls == ["primary"]


def test_fastapi_summary_returns_429_on_usage_limit_and_502_scrubs_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "sk-env-secret-should-not-leak")
    todo_mod = _load_todo_app()
    store = wire_fake_todo_pg(monkeypatch)
    _seed_store(store)
    client = TestClient(todo_mod.fastapi_app)

    async def _raise_limit(**_kwargs: Any) -> Any:
        raise UsageLimitExceeded("output_tokens_limit=10 exceeded")

    monkeypatch.setattr(todo_mod, "summarize_recent_todos", _raise_limit)
    resp_429 = client.post("/api/todos/summary", json={"limit": 5})
    assert resp_429.status_code == 429
    assert resp_429.json()["detail"] == "UsageLimitExceeded"

    # Restore and test HTTP 502 error scrubbing + InstrumentedModel span exception scrubbing.
    todo_mod = _load_todo_app()
    client = TestClient(todo_mod.fastapi_app)
    store = wire_fake_todo_pg(monkeypatch)
    _seed_store(store)

    leaked_token = "sk-live-secret-99999"
    leaked_param = "zai-secret-88888"

    def _fail_upstream(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        req = httpx.Request("POST", f"https://api.z.ai/v4/chat/completions?api_key={leaked_param}")
        resp = httpx.Response(
            401,
            request=req,
            json={"error": f"bad auth Bearer {leaked_token}"},
        )
        raise openai.AuthenticationError(
            f"Bearer {leaked_token} api_key={leaked_param}",
            response=resp,
            body=None,
        )

    span_exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(span_exporter))

    orig_summarize = todo_mod.summarize_recent_todos

    async def _call_with_failing_model(**kwargs: Any) -> Any:
        return await orig_summarize(
            **{
                **kwargs,
                "model_override": FunctionModel(_fail_upstream),
                "tracer_provider": tracer_provider,
                "max_attempts": 1,
            }
        )

    monkeypatch.setattr(todo_mod, "summarize_recent_todos", _call_with_failing_model)
    resp_502 = client.post("/api/todos/summary", json={"limit": 5})
    assert resp_502.status_code == 502
    body_text = resp_502.text
    assert leaked_token not in body_text
    assert leaked_param not in body_text

    scrubbed = redact_llm_error(ValueError(f"Bearer {leaked_token} api_key={leaked_param}"))
    assert leaked_token not in str(scrubbed)
    assert leaked_param not in str(scrubbed)

    events_dump = json.dumps(
        [
            {"name": ev.name, "attrs": dict(ev.attributes or {})}
            for s in span_exporter.get_finished_spans()
            for ev in s.events
        ],
        default=str,
    )
    assert leaked_token not in events_dump
    assert leaked_param not in events_dump
    tracer_provider.shutdown()


@pytest.mark.vcr
async def test_todo_summary_vcr_zai_non_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "zai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
    monkeypatch.setenv("LLM_SMALL_MODEL", "glm-5.3-flash")
    monkeypatch.setenv("LLM_API_KEY", _AMBIENT_RECORD_API_KEY or "vcr-replay-placeholder")

    todo_mod = _load_todo_app()
    with allow_vcr_model_requests():
        res = await todo_mod.summarize_recent_todos(
            limit=5,
            stream=False,
            todos_loader=lambda _n: SAMPLE_TODOS,
            max_attempts=1,
        )

    assert res.status == "pass"
    assert res.model == "glm-5.3-flash"
    assert res.todo_count == 2
    assert res.input_tokens > 0
    assert res.output_tokens > 0
    assert res.summary is not None
    assert res.summary.headline.strip()
    assert res.summary.focus.strip()
    assert res.summary.open_count >= 0
    assert res.summary.completed_count >= 0


@pytest.mark.vcr
async def test_todo_summary_vcr_zai_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "zai")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.z.ai/api/coding/paas/v4")
    monkeypatch.setenv("LLM_SMALL_MODEL", "glm-5.3-flash")
    monkeypatch.setenv("LLM_API_KEY", _AMBIENT_RECORD_API_KEY or "vcr-replay-placeholder")

    todo_mod = _load_todo_app()
    with allow_vcr_model_requests():
        res = await todo_mod.summarize_recent_todos(
            limit=5,
            stream=True,
            todos_loader=lambda _n: SAMPLE_TODOS,
            max_attempts=1,
        )

    assert res.status == "pass"
    assert res.model == "glm-5.3-flash"
    assert res.todo_count == 2
    assert res.input_tokens > 0
    assert res.output_tokens > 0
    assert res.summary is not None
    assert res.summary.headline.strip()
    assert res.summary.focus.strip()
