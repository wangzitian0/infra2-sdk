"""Tests for pydantic-ai Agent factory, DecodingSpec, OTel instrumentation, redaction, and VCR."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import openai
import pytest
import vcr
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import BaseModel
from pydantic_ai import UsageLimits, models
from pydantic_ai.exceptions import (
    ModelConnectionError,
    ModelHTTPError,
    ModelRateLimitError,
    ModelRequestTimeout,
    ModelServerError,
    UsageLimitExceeded,
    _HTTPModelRateLimitError,
)
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.instrumented import InstrumentationSettings, InstrumentedModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.models.wrapper import WrapperModel

from infra2_sdk.runtime.llm import (
    DecodingSpec,
    GuardedLlmModel,
    LlmClient,
    LlmSettings,
    allow_vcr_model_requests,
    create_llm_agent,
    create_llm_model,
    llm_call_context,
    prompt_sha256,
    redact_llm_error,
    redact_uri,
    request_sha256,
    sanitize_headers,
    schema_sha256,
    vcr_llm_config,
)
from infra2_sdk.runtime.otel import (
    _JsonLogFormatter,
    _logging_handler_class,
    _RedactingLogRecordProcessor,
)


class _SampleOutput(BaseModel):
    answer: str
    score: int


def test_decoding_spec_has_no_response_format_and_validates() -> None:
    spec = DecodingSpec(
        "v2", thinking=False, temperature=0.1, top_p=0.9, seed=7, max_tokens=256, timeout=15.0
    )
    assert not hasattr(spec, "response_format")
    assert spec.to_model_settings() == {
        "thinking": False,
        "temperature": 0.1,
        "top_p": 0.9,
        "seed": 7,
        "max_tokens": 256,
        "timeout": 15.0,
    }
    assert DecodingSpec().to_model_settings() == {}
    with pytest.raises(ValueError, match="decoding_version"):
        DecodingSpec("  ")
    with pytest.raises(ValueError, match="max_tokens"):
        DecodingSpec(max_tokens=0)
    with pytest.raises(ValueError, match="max_tokens"):
        DecodingSpec(max_tokens=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timeout"):
        DecodingSpec(timeout=0.0)


def test_deterministic_hashing_helpers() -> None:
    p1 = prompt_sha256({"b": 2, "a": [1, {"y": "z", "x": "w"}]})
    p2 = prompt_sha256({"a": [1, {"x": "w", "y": "z"}], "b": 2})
    assert p1 == p2 and len(p1) == 64

    s1 = schema_sha256(_SampleOutput)
    s2 = schema_sha256(_SampleOutput.model_json_schema())
    assert s1 == s2 and len(s1) == 64
    with pytest.raises(ValueError, match="schema is required"):
        schema_sha256(None)

    r1 = request_sha256(
        model="glm-5.3-flash",
        prompt=p1,
        schema=_SampleOutput,
        decoding=DecodingSpec("v1", thinking=False),
        extra={"b": 2, "a": 1},
    )
    r2 = request_sha256(
        model="glm-5.3-flash",
        prompt=p2,
        schema=_SampleOutput.model_json_schema(),
        decoding={
            "decoding_version": "v1",
            "thinking": False,
            "temperature": None,
            "top_p": None,
            "seed": None,
            "max_tokens": None,
            "timeout": None,
        },
        extra={"a": 1, "b": 2},
    )
    assert r1 == r2
    assert r1 != request_sha256(model="glm-5.3", prompt=p1, schema=_SampleOutput)
    with pytest.raises(ValueError, match="model must be non-empty"):
        request_sha256(model=" ", prompt="hi")


def test_redact_uri_and_sanitize_headers() -> None:
    uri = "https://user:supersecret@api.example.com:8443/v1/chat?api_key=sk-123&model=glm&token=tok"
    assert (
        redact_uri(uri)
        == "https://api.example.com:8443/v1/chat?api_key=REDACTED&model=glm&token=REDACTED"
    )
    ipv6 = "http://admin:pass@[::1]:8080/v1?secret=abc&ok=1"
    assert redact_uri(ipv6) == "http://[::1]:8080/v1?secret=REDACTED&ok=1"

    cleaned = sanitize_headers(
        {
            "Authorization": "Bearer sk-abc123",
            "x-api-key": "key-xyz",
            "retry-after": "5",
            "x-should-retry": "false",
            "Content-Type": "application/json",
        }
    )
    assert cleaned == {
        "Authorization": "REDACTED",
        "x-api-key": "REDACTED",
        "retry-after": "5",
        "x-should-retry": "false",
        "Content-Type": "application/json",
    }


def test_redact_llm_error_preserves_subclasses_and_allows_fallback() -> None:
    http_rl = _HTTPModelRateLimitError(
        status_code=429,
        model_name="glm-5.3-flash",
        body={"error": {"code": "1302", "message": "SUPER_SECRET_PROMPT alice@example.com"}},
        headers={
            "Authorization": "Bearer sk-secret",
            "retry-after": "2",
            "x-should-retry": "false",
        },
        provider_error_code="1302",
        retry_after=2.0,
    )
    scrubbed_rl = redact_llm_error(http_rl)
    assert isinstance(scrubbed_rl, ModelRateLimitError)
    assert isinstance(scrubbed_rl, ModelHTTPError)
    assert scrubbed_rl.provider_error_code == "1302"
    assert scrubbed_rl.retry_after == 2.0
    assert scrubbed_rl.provider_retry_hint is False
    assert scrubbed_rl.headers["authorization"] == "REDACTED"
    assert "SUPER_SECRET_PROMPT" not in str(scrubbed_rl)
    assert "alice@example.com" not in str(scrubbed_rl)

    srv = redact_llm_error(ModelServerError("glm-5.3", "boom sk-secret99999"))
    assert type(srv) is ModelServerError
    assert "sk-secret99999" not in str(srv)

    conn = redact_llm_error(ModelConnectionError("glm-5.3", "leak", phase="connect"))
    assert type(conn) is ModelConnectionError and conn.phase == "connect"

    tmo = redact_llm_error(ModelRequestTimeout("glm-5.3", 12.5, setting="request_timeout"))
    assert type(tmo) is ModelRequestTimeout and tmo.timeout == 12.5

    other = redact_llm_error(ValueError("sk-secret user@example.com"))
    assert type(other) is RuntimeError and "sk-secret" not in str(other)

    # Prove FallbackModel falls back cleanly after GuardedLlmModel scrubs a ModelHTTPError.
    def fail_http(_m: list[Any], _info: Any) -> ModelResponse:
        raise ModelHTTPError(
            status_code=503,
            model_name="m1",
            body={"raw": "SUPER_SECRET_PROMPT"},
            provider_error_code="5003",
        )

    def ok_fb(_m: list[Any], _info: Any) -> ModelResponse:
        return ModelResponse(parts=[TextPart("fallback-ok")], model_name="m2")

    fb = FallbackModel(
        GuardedLlmModel(FunctionModel(fail_http, model_name="m1"), call_site="fb.test"),
        GuardedLlmModel(FunctionModel(ok_fb, model_name="m2"), call_site="fb.test"),
    )
    agent = create_llm_agent(model=fb, call_site="fb.test", max_attempts=1)
    assert agent.run_sync("SUPER_SECRET_PROMPT").output == "fallback-ok"


def test_create_llm_model_configures_explicit_credentials_zero_sdk_retries_and_stream_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ZAI_API_KEY", "ambient-wrong-key")
    with pytest.raises(ValueError, match="LlmSettings.api_key is required"):
        create_llm_model(LlmSettings(provider="zai", api_key=None), instrument=False, guard=False)

    zai = create_llm_model(
        LlmSettings(
            provider="zai", api_key="explicit-zai-key", base_url="https://custom.zai.test/v4"
        ),
        instrument=False,
        guard=False,
    )
    assert zai.client.api_key == "explicit-zai-key"
    assert str(zai.client.base_url).rstrip("/") == "https://custom.zai.test/v4"
    assert zai.client.max_retries == 0
    assert zai._get_stream_options({}) == {"include_usage": True}

    zai_no_usage = create_llm_model(
        LlmSettings(provider="zai", api_key="explicit-zai-key"),
        include_stream_usage=False,
        instrument=False,
        guard=False,
    )
    assert zai_no_usage._get_stream_options({}) is openai.omit

    oai = create_llm_model(
        LlmSettings(provider="openai", api_key="oai-key"),
        instrument=False,
        guard=False,
    )
    assert oai.client.max_retries == 0
    assert "api.z.ai" not in str(oai.client.base_url)

    or_mod = create_llm_model(
        LlmSettings(provider="openrouter", api_key="or-key", small_model="z-ai/glm-5.3-flash"),
        instrument=False,
        guard=False,
    )
    assert or_mod.client.max_retries == 0
    assert "openrouter.ai" in str(or_mod.client.base_url)

    or_no_usage = create_llm_model(
        LlmSettings(provider="openrouter", api_key="or-key", small_model="z-ai/glm-5.3-flash"),
        include_stream_usage=False,
        instrument=False,
        guard=False,
    )
    assert or_no_usage._get_stream_options({}) is openai.omit


def test_create_llm_agent_otel_traces_metrics_logs_and_multiple_sync_and_stream_runs() -> None:
    import pydantic_ai

    secret_prompt = "SECRET_PROMPT_DO_NOT_LEAK_USER_XYZ"
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    metric_reader = InMemoryMetricReader()
    mp = MeterProvider(metric_readers=[metric_reader])
    log_exporter = InMemoryLogRecordExporter()
    lp = LoggerProvider()
    lp.add_log_record_processor(_RedactingLogRecordProcessor())
    lp.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))

    json_fmt = _JsonLogFormatter(
        {
            "service.name": "todo",
            "deployment.environment.name": "staging",
            "service.version": "3.34.0",
        }
    )
    logger = logging.getLogger("infra2.test.llm.agent")
    logger.setLevel(logging.INFO)
    logger.handlers = [_logging_handler_class()(level=logging.NOTSET, logger_provider=lp)]
    logger.propagate = False
    stdout_logs: list[dict[str, Any]] = []

    def tool_and_respond(messages: list[Any], _info: Any) -> ModelResponse:
        rec = logging.LogRecord(
            "infra2.test.llm.agent", logging.INFO, __file__, 1, "inside call", (), None
        )
        stdout_logs.append(json.loads(json_fmt.format(rec)))
        logger.info("inside model call")
        return ModelResponse(parts=[TextPart("pong")], model_name="fake-model")

    async def stream_respond(messages: list[Any], _info: Any) -> AsyncIterator[str]:
        rec = logging.LogRecord(
            "infra2.test.llm.agent", logging.INFO, __file__, 1, "inside stream", (), None
        )
        stdout_logs.append(json.loads(json_fmt.format(rec)))
        logger.info("inside stream model call")
        yield "pong"

    class CustomGateWrapper(WrapperModel):
        entered: int = 0

        async def request(self, messages: list[Any], ms: Any, mrp: Any) -> ModelResponse:
            CustomGateWrapper.entered += 1
            return await super().request(messages, ms, mrp)

    gated = CustomGateWrapper(
        FunctionModel(tool_and_respond, stream_function=stream_respond, model_name="fake-model")
    )
    agent = create_llm_agent(
        model=gated,
        call_site="todo.summary",
        decoding=DecodingSpec("v2", thinking=False, max_tokens=64),
        tracer_provider=tp,
        meter_provider=mp,
    )

    try:
        buf = io.StringIO()
        with (
            contextlib.redirect_stdout(buf),
            contextlib.redirect_stderr(buf),
            llm_call_context(domain_run_id="run-99", prompt_sha256="a" * 64),
        ):
            r1 = agent.run_sync(secret_prompt)
            r2 = agent.run_sync(secret_prompt)
            assert r1.output == "pong" and r2.output == "pong"
        assert pydantic_ai.BANNER_ENABLED is False
        assert buf.getvalue() == ""
        assert CustomGateWrapper.entered == 2

        async def stream_check() -> str:
            with llm_call_context(domain_run_id="run-99", prompt_sha256="a" * 64):
                async with agent.run_stream(secret_prompt) as stream_res:
                    return await stream_res.get_output()

        assert asyncio.run(stream_check()) == "pong"

        spans = exporter.get_finished_spans()
        invoke_spans = [s for s in spans if s.name == "invoke_agent todo.summary"]
        chat_spans = [s for s in spans if s.name.startswith("chat ")]
        assert len(invoke_spans) == 3
        assert len(chat_spans) == 3
        for span in [*invoke_spans, *chat_spans]:
            s_attrs = dict(span.attributes or {})
            assert secret_prompt not in json.dumps(s_attrs)
            assert s_attrs["infra.llm.call_site"] == "todo.summary"
            assert s_attrs["infra.llm.decoding_version"] == "v2"
            assert s_attrs["code.file.path"] == "tests/test_llm.py"
            assert "/Users/" not in str(s_attrs["code.file.path"])
            assert s_attrs["code.function.name"] in {
                "test_create_llm_agent_otel_traces_metrics_logs_and_multiple_sync_and_stream_runs",
                "stream_check",
            }
            assert "gen_ai.request.parameters" not in s_attrs

        # Check stdout JSON and OTLP logs carry matching dotted keys for each invocation
        lp.force_flush()
        otlp_logs = log_exporter.get_finished_logs()
        assert len(stdout_logs) == 3 and len(otlp_logs) == 3
        for idx, (jlog, olog) in enumerate(zip(stdout_logs, otlp_logs, strict=True)):
            oattrs = dict(olog.log_record.attributes or {})
            for key in (
                "infra.llm.call_site",
                "infra.llm.decoding_version",
                "code.file.path",
                "code.function.name",
                "code.line.number",
                "infra.llm.domain_run_id",
            ):
                assert jlog[key] == oattrs[key] == invoke_spans[idx].attributes[key]

        # Check duration & single-recorded token metrics have strictly low-cardinality labels
        mdata = metric_reader.get_metrics_data()
        hist_points = [
            dp
            for rm in mdata.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
            if m.name == "gen_ai.client.operation.duration"
            for dp in m.data.data_points
        ]
        assert len(hist_points) == 1
        assert hist_points[0].count == 3
        token_metrics = [
            dp
            for rm in mdata.resource_metrics
            for sm in rm.scope_metrics
            for m in sm.metrics
            if m.name == "gen_ai.client.token.usage"
            for dp in m.data.data_points
        ]
        assert len(token_metrics) == 2
        assert sum(dp.sum for dp in token_metrics) == sum(
            (s.attributes.get("gen_ai.usage.input_tokens", 0) or 0)
            + (s.attributes.get("gen_ai.usage.output_tokens", 0) or 0)
            for s in chat_spans
        )
        for dp in hist_points:
            mattrs = dict(dp.attributes or {})
            assert mattrs["infra.llm.call_site"] == "todo.summary"
            assert mattrs["infra.llm.decoding_version"] == "v2"
            assert "infra.llm.domain_run_id" not in mattrs
            assert "infra.llm.prompt_sha256" not in mattrs
            assert "code.line.number" not in mattrs
    finally:
        logger.handlers = []
        lp.shutdown()
        tp.shutdown()
        mp.shutdown()


def test_missing_call_site_emits_warning_and_falls_back_to_module_function(
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    agent = create_llm_agent(model=TestModel(), tracer_provider=tp)

    with caplog.at_level(logging.WARNING, logger="infra2_sdk.runtime.llm"):
        res = agent.run_sync("hello")
    assert res.output

    warnings = [r for r in caplog.records if "missing explicit call_site" in r.getMessage()]
    assert len(warnings) == 1
    expected_site = "test_llm.test_missing_call_site_emits_warning_and_falls_back_to"
    assert len(expected_site) <= 64
    assert expected_site in warnings[0].getMessage()
    invoke = [s for s in exporter.get_finished_spans() if s.name.startswith("invoke_agent ")]
    assert invoke and invoke[0].name == f"invoke_agent {expected_site}"
    assert invoke[0].attributes["infra.llm.call_site"] == expected_site
    tp.shutdown()


def test_retry_on_model_errors_and_usage_limits_bypass_fallback() -> None:
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))

    # 1. Single-layer RetryOnModelErrors retries once, scrubbing error before span records it.
    attempts = 0

    def flaky(_m: list[Any], _i: Any) -> ModelResponse:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ModelRateLimitError("m1", "leaked SECRET_KEY_XYZ", provider_error_code="1302")
        return ModelResponse(parts=[TextPart("recovered")], model_name="m1")

    agent = create_llm_agent(
        model=FunctionModel(flaky, model_name="m1"),
        call_site="retry.test",
        max_attempts=2,
        initial_delay=0.0,
        max_delay=0.0,
        tracer_provider=tp,
    )
    assert agent.run_sync("SECRET_KEY_XYZ").output == "recovered"
    assert attempts == 2
    finished = exporter.get_finished_spans()
    attempt_spans = [s for s in finished if s.name.startswith("model request attempt ")]
    chat_spans = [s for s in finished if s.name.startswith("chat ")]
    assert len(attempt_spans) == 1
    assert len(chat_spans) == 1
    for sp in finished:
        assert "SECRET_KEY_XYZ" not in json.dumps(dict(sp.attributes or {}))
        for ev in sp.events:
            assert "SECRET_KEY_XYZ" not in str(dict(ev.attributes or {}))

    # 2. UsageLimitExceeded bypasses both RetryOnModelErrors and ModelFallback immediately.
    primary_calls = 0
    fallback_calls = 0

    def raise_usage_limit(_m: list[Any], _i: Any) -> ModelResponse:
        nonlocal primary_calls
        primary_calls += 1
        raise UsageLimitExceeded("budget exhausted")

    def fb_model(_m: list[Any], _i: Any) -> ModelResponse:
        nonlocal fallback_calls
        fallback_calls += 1
        return ModelResponse(parts=[TextPart("should-not-run")], model_name="fb")

    limited_agent = create_llm_agent(
        model=FunctionModel(raise_usage_limit, model_name="primary"),
        fallback_models=(FunctionModel(fb_model, model_name="fb"),),
        call_site="limit.test",
        max_attempts=3,
        initial_delay=0.0,
        max_delay=0.0,
    )
    with pytest.raises(UsageLimitExceeded):
        limited_agent.run_sync("go")
    assert primary_calls == 1 and fallback_calls == 0

    # 3. Default usage_limits on create_llm_agent applies across run_sync.
    default_limited = create_llm_agent(
        model=TestModel(),
        call_site="limit.default",
        usage_limits=UsageLimits(request_limit=0),
    )
    with pytest.raises(UsageLimitExceeded):
        default_limited.run_sync("blocked before dispatch")
    tp.shutdown()


def test_vcr_llm_config_and_allow_vcr_model_requests_with_zai_httpx2(tmp_path: Path) -> None:
    payload = json.dumps(
        {
            "id": "chatcmpl-vcr",
            "object": "chat.completion",
            "created": 1700000000,
            "model": "glm-5.3-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "vcr-replayed-ok"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
        }
    )
    cassette = tmp_path / "zai_chat.yaml"
    cassette.write_text(
        "version: 1\ninteractions:\n- request:\n    body: null\n    headers: {}\n"
        "    method: POST\n    uri: https://api.z.ai/api/coding/paas/v4/chat/completions\n"
        f"  response:\n    body:\n      string: {json.dumps(payload)}\n"
        "    headers:\n      content-type:\n      - application/json\n"
        "    status:\n      code: 200\n      message: OK\n",
        encoding="utf-8",
    )

    cfg = vcr_llm_config(cassette_library_dir=tmp_path, record_mode="none")
    assert "match_on" in cfg and "method" in cfg["match_on"]

    # Default vcr_llm_config omits record_mode so pytest --record-mode CLI is not overridden.
    fixture_cfg = vcr_llm_config()
    assert "record_mode" not in fixture_cfg

    # Scrubbing hooks strip Authorization, Cookie, and URI api_key query param.
    from vcr.request import Request

    raw_req = Request(
        "POST",
        "https://user:pass@api.z.ai/api/coding/paas/v4/chat/completions?api_key=sk-123&model=glm",
        b"{}",
        {"Authorization": "Bearer sk-123", "x-should-retry": "true"},
    )
    scrubbed_req = cfg["before_record_request"](raw_req)
    assert "sk-123" not in scrubbed_req.uri and "user:pass" not in scrubbed_req.uri
    assert scrubbed_req.headers["Authorization"] == "REDACTED"
    assert scrubbed_req.headers["x-should-retry"] == "true"

    scrubbed_resp = cfg["before_record_response"](
        {"headers": {"Set-Cookie": ["session=secret"], "Content-Type": ["application/json"]}}
    )
    assert scrubbed_resp["headers"]["Set-Cookie"] == ["REDACTED"]
    assert scrubbed_resp["headers"]["Content-Type"] == ["application/json"]

    # Build real ZaiModel agent and test ALLOW_MODEL_REQUESTS=False + VCR httpx2 replay.
    agent = create_llm_agent(
        LlmSettings(provider="zai", api_key="test-key"),
        role="small",
        call_site="vcr.test",
        max_attempts=1,
    )
    my_vcr = vcr.VCR(
        **{
            **cfg,
            "match_on": ("method", "scheme", "host", "path"),
        }
    )

    with models.override_allow_model_requests(False):
        with pytest.raises(RuntimeError, match="ALLOW_MODEL_REQUESTS"):
            agent.run_sync("outside vcr")

        with allow_vcr_model_requests(), my_vcr.use_cassette(str(cassette)):
            res = agent.run_sync("replayed")
            assert res.output == "vcr-replayed-ok"

            # Unrecorded host inside record_mode=none fails closed (never touches network).
            from vcr.errors import CannotOverwriteExistingCassetteException

            unrecorded = create_llm_agent(
                LlmSettings(provider="zai", api_key="k", base_url="https://unrecorded.invalid/v4"),
                call_site="vcr.miss",
                max_attempts=1,
            )
            with pytest.raises(CannotOverwriteExistingCassetteException):
                unrecorded.run_sync("blocked unrecorded")


def test_run_stream_sync_and_run_stream_events_and_nested_agents() -> None:
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    json_fmt = _JsonLogFormatter({"service.name": "todo"})

    async def stream_ok(_m: list[Any], _info: Any) -> AsyncIterator[str]:
        yield "stream-ok"

    stream_agent = create_llm_agent(
        model=FunctionModel(
            lambda _m, _i: ModelResponse(parts=[TextPart("ok")], model_name="fake"),
            stream_function=stream_ok,
            model_name="fake",
        ),
        call_site="stream.site",
        decoding=DecodingSpec("v3", thinking=False),
        tracer_provider=tp,
    )

    def sync_caller() -> str:
        with stream_agent.run_stream_sync("SECRET_STREAM_PROMPT") as res:
            chunks = list(res.stream_text())
            assert res.get_output() == "stream-ok"
            return "".join(chunks)

    assert sync_caller() == "stream-ok"

    async def events_caller() -> None:
        async with stream_agent.run_stream_events("SECRET_EVENT_PROMPT") as events:
            async for _event in events:
                pass

    asyncio.run(events_caller())

    tool_logs: list[dict[str, Any]] = []
    inner_agent = create_llm_agent(
        model=TestModel(custom_output_text="inner-ok"),
        call_site="inner.site",
        tracer_provider=tp,
    )
    outer_agent = create_llm_agent(
        model=TestModel(call_tools=["delegate"]),
        call_site="agent.site",
        decoding=DecodingSpec("v2"),
        tracer_provider=tp,
    )

    @outer_agent.tool_plain
    async def delegate() -> str:
        rec = logging.LogRecord("infra2.test.tool", logging.INFO, __file__, 1, "in tool", (), None)
        tool_logs.append(json.loads(json_fmt.format(rec)))
        res = await inner_agent.run("subtask")
        return str(res.output)

    with llm_call_context(call_site="outer.ctx", decoding_version="v1"):
        outer_res = outer_agent.run_sync("SECRET_NESTED_PROMPT")
    assert outer_res.output

    assert len(tool_logs) == 1
    assert tool_logs[0]["infra.llm.call_site"] == "agent.site"
    assert tool_logs[0]["infra.llm.decoding_version"] == "v2"

    spans = exporter.get_finished_spans()
    for span in spans:
        assert "SECRET_" not in json.dumps(dict(span.attributes or {}))
    invokes = {
        s.name: dict(s.attributes or {}) for s in spans if s.name.startswith("invoke_agent ")
    }
    assert invokes["invoke_agent inner.site"]["code.function.name"] == "delegate"
    assert invokes["invoke_agent agent.site"]["infra.llm.call_site"] == "agent.site"

    stream_invokes = [
        dict(s.attributes or {}) for s in spans if s.name == "invoke_agent stream.site"
    ]
    assert {attrs["code.function.name"] for attrs in stream_invokes} == {
        "sync_caller",
        "events_caller",
    }
    tp.shutdown()


def test_nested_and_fallback_instrumented_models_do_not_double_count_or_leak() -> None:
    # 1. create_llm_agent with InstrumentedModel in fallback_models emits 1 chat span + 1x tokens.
    exp1 = InMemorySpanExporter()
    tp1 = TracerProvider()
    tp1.add_span_processor(SimpleSpanProcessor(exp1))
    mr1 = InMemoryMetricReader()
    mp1 = MeterProvider(metric_readers=[mr1])

    def fail_primary(_m: list[Any], _i: Any) -> ModelResponse:
        raise ModelRateLimitError("primary", "fail", provider_error_code="429")

    fb_inst = create_llm_model(
        model=FunctionModel(
            lambda _m, _i: ModelResponse(parts=[TextPart("fb-ok")], model_name="fb-m"),
            model_name="fb-m",
        ),
        call_site="fb.wrapped",
        tracer_provider=tp1,
        meter_provider=mp1,
    )
    ag1 = create_llm_agent(
        model=FunctionModel(fail_primary, model_name="primary"),
        fallback_models=(fb_inst,),
        call_site="agent.fb",
        max_attempts=1,
        tracer_provider=tp1,
        meter_provider=mp1,
    )
    assert ag1.run_sync("SECRET_FB_PROMPT").output == "fb-ok"
    chats1 = [s for s in exp1.get_finished_spans() if s.name.startswith("chat ")]
    assert len(chats1) == 1
    assert "SECRET_FB_PROMPT" not in json.dumps(dict(chats1[0].attributes or {}))
    tok_pts1 = [
        dp
        for rm in mr1.get_metrics_data().resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "gen_ai.client.token.usage"
        for dp in m.data.data_points
    ]
    assert len(tok_pts1) == 2
    assert all(dp.count == 1 for dp in tok_pts1)

    # 2. create_llm_model(model=create_llm_model(...), fallback_models=[...]) instruments once.
    exp2 = InMemorySpanExporter()
    tp2 = TracerProvider()
    tp2.add_span_processor(SimpleSpanProcessor(exp2))
    mr2 = InMemoryMetricReader()
    mp2 = MeterProvider(metric_readers=[mr2])

    wrapped_primary = create_llm_model(
        model=TestModel(),
        call_site="m.primary",
        tracer_provider=tp2,
        meter_provider=mp2,
    )
    combined_model = create_llm_model(
        model=wrapped_primary,
        fallback_models=(TestModel(),),
        call_site="m.combined",
        tracer_provider=tp2,
        meter_provider=mp2,
    )
    ag2 = create_llm_agent(
        model=combined_model,
        call_site="agent.combined",
        tracer_provider=tp2,
        meter_provider=mp2,
    )
    assert ag2.run_sync("SECRET_COMBINED_PROMPT").output
    chats2 = [s for s in exp2.get_finished_spans() if s.name.startswith("chat ")]
    assert len(chats2) == 1
    tok_pts2 = [
        dp
        for rm in mr2.get_metrics_data().resource_metrics
        for sm in rm.scope_metrics
        for m in sm.metrics
        if m.name == "gen_ai.client.token.usage"
        for dp in m.data.data_points
    ]
    assert len(tok_pts2) == 2
    assert all(dp.count == 1 for dp in tok_pts2)

    # 3. GuardedLlmModel(InstrumentedModel(..., include_content=True)) strips unsafe inner layer.
    inner_exp = InMemorySpanExporter()
    inner_tp = TracerProvider()
    inner_tp.add_span_processor(SimpleSpanProcessor(inner_exp))
    outer_exp = InMemorySpanExporter()
    outer_tp = TracerProvider()
    outer_tp.add_span_processor(SimpleSpanProcessor(outer_exp))

    unsafe = GuardedLlmModel(
        InstrumentedModel(
            TestModel(),
            InstrumentationSettings(tracer_provider=inner_tp, include_content=True),
        ),
        call_site="inner.unsafe",
    )
    ag3 = create_llm_agent(
        model=unsafe,
        call_site="outer.safe",
        tracer_provider=outer_tp,
    )
    assert ag3.run_sync("SECRET_XYZ_NESTED").output
    assert len(inner_exp.get_finished_spans()) == 0
    outer_chats = [s for s in outer_exp.get_finished_spans() if s.name.startswith("chat ")]
    assert len(outer_chats) == 1
    assert "SECRET_XYZ_NESTED" not in json.dumps(dict(outer_chats[0].attributes or {}))

    for prov in (tp1, mp1, tp2, mp2, inner_tp, outer_tp):
        prov.shutdown()


def test_auth_4xx_model_http_error_is_not_retried_by_retry_on_model_errors() -> None:
    attempts = 0

    def unauthorized(_m: list[Any], _i: Any) -> ModelResponse:
        nonlocal attempts
        attempts += 1
        raise ModelHTTPError(401, "m1", {"error": "invalid api key"})

    agent = create_llm_agent(
        model=FunctionModel(unauthorized, model_name="m1"),
        call_site="auth.noretry",
        max_attempts=3,
        initial_delay=0.0,
        max_delay=0.0,
    )
    with pytest.raises(ModelHTTPError) as exc_info:
        agent.run_sync("hello")
    assert exc_info.value.status_code == 401
    assert attempts == 1


def test_failed_retry_attempts_get_attempt_spans_and_success_gets_one_chat_span() -> None:
    exporter = InMemorySpanExporter()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(exporter))
    attempts = 0

    def flaky(_m: list[Any], _i: Any) -> ModelResponse:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ModelRateLimitError("m1", "slow down", provider_error_code="1302")
        return ModelResponse(parts=[TextPart("ok")], model_name="m1")

    agent = create_llm_agent(
        model=FunctionModel(flaky, model_name="m1"),
        call_site="retry.spans",
        max_attempts=3,
        initial_delay=0.0,
        max_delay=0.0,
        tracer_provider=tp,
    )
    assert agent.run_sync("x").output == "ok"
    assert attempts == 3
    names = [s.name for s in exporter.get_finished_spans()]
    assert names.count("model request attempt m1") == 2
    assert sum(n.startswith("chat ") for n in names) == 1
    tp.shutdown()


def test_anthropic_model_disables_vendor_retries() -> None:
    pytest.importorskip(
        "anthropic", reason="infra2-sdk[pydantic-ai] extra lacks the anthropic package"
    )
    model = create_llm_model(
        LlmSettings(provider="anthropic", api_key="anth-key"), instrument=False, guard=False
    )
    assert model.client.max_retries == 0


def test_google_model_allows_one_vendor_attempt() -> None:
    pytest.importorskip(
        "google.genai", reason="infra2-sdk[pydantic-ai] extra lacks the google-genai package"
    )
    model = create_llm_model(
        LlmSettings(provider="google", api_key="goog-key"), instrument=False, guard=False
    )
    retry = model.client._api_client._http_options.retry_options
    assert retry is not None and retry.attempts == 1


def test_llm_client_construction_emits_deprecation_warning_at_caller() -> None:
    with pytest.warns(DeprecationWarning, match="create_llm_agent") as record:
        LlmClient(LlmSettings(api_key="k"))
    assert record[0].filename == __file__
