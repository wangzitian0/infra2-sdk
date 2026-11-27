"""Tests for infra2_sdk.llm."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from decimal import Decimal

import httpx
import pytest
from pydantic import BaseModel
from pydantic_ai import UsageLimits
from pydantic_ai.exceptions import (
    ModelConnectionError,
    ModelHTTPError,
    ModelRequestTimeout,
    UnexpectedModelBehavior,
    UsageLimitExceeded,
)
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage

from infra2_sdk import llm


class _Out(BaseModel):
    answer: str


def test_llm_settings_decoding_and_binary_digests() -> None:
    cfg = llm.LlmSettings.from_env(
        {
            "LLM_PROVIDER": "zai",
            "LLM_API_KEY": "sk-test-secret",
            "LLM_SMALL_MODEL": "glm-5.3-flash",
            "LLM_LARGE_MODEL": "glm-5.3",
        }
    )
    assert cfg.role_model("small") == "glm-5.3-flash"
    assert cfg.role_model("large") == "glm-5.3"
    assert "sk-test-secret" not in repr(cfg)
    with pytest.raises(ValueError, match="unknown model role"):
        cfg.role_model("unknown")  # type: ignore[arg-type]

    dec = llm.DecodingSpec(decoding_version="demo.v1", temperature=0.0, max_tokens=128)
    assert dec.to_model_settings() == {"temperature": 0.0, "max_tokens": 128}
    with pytest.raises(ValueError):
        llm.DecodingSpec(temperature=float("nan"))
    with pytest.raises(ValueError):
        llm.DecodingSpec(seed=True)  # type: ignore[arg-type]

    s_hash = llm.schema_sha256(_Out)
    assert len(s_hash) == 64


def test_redact_errors_passthrough_and_streaming(caplog: pytest.LogCaptureFixture) -> None:
    err = ModelHTTPError(
        status_code=401,
        model_name="glm-5.3-flash",
        body={"error": {"code": "bad_auth", "message": "Bearer sk-secret-123"}},
    )
    scrubbed = llm.redact_llm_error(err)
    assert isinstance(scrubbed, ModelHTTPError) and "sk-secret-123" not in str(scrubbed)
    assert isinstance(
        llm.redact_llm_error(ModelRequestTimeout("m", 5.0, setting="timeout")), ModelRequestTimeout
    )
    assert isinstance(
        llm.redact_llm_error(ModelConnectionError("m", "secret", phase="request")),
        ModelConnectionError,
    )
    assert "sk-secret-456" not in str(
        llm.redact_llm_error(httpx.ConnectError("Bearer sk-secret-456"))
    )

    settings = llm.LlmSettings(provider="zai", api_key="test-key")
    agent = llm.create_llm_agent(
        settings,
        model=TestModel(custom_output_args={"answer": "ok"}),
        output_type=_Out,
        call_site="unit.echo",
        decoding=llm.DecodingSpec(decoding_version="unit.v1"),
    )
    with llm.llm_call_context(call_site="unit.echo", prompt_sha256="a" * 64):
        assert agent.run_sync("ping").output == _Out(answer="ok")

    def _bad_output(_msgs: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise UnexpectedModelBehavior("malformed tool")

    bad_out_agent = llm.create_llm_agent(
        settings, model=FunctionModel(_bad_output), call_site="unit.ctl", max_attempts=1
    )
    with pytest.raises(UnexpectedModelBehavior):
        bad_out_agent.run_sync("go")

    class _CallerStreamError(ValueError):
        pass

    async def _async_paths() -> None:
        run_r = await agent.run("ping")
        assert run_r.output.answer == "ok"
        async with agent.run_stream("ping") as s:
            assert (await s.get_output()).answer == "ok"
        async with agent.iter("ping") as it:
            async for _ in it:
                pass
            assert it.result is not None
        async with agent.run_stream_events("ping") as events:
            collected = [ev async for ev in events]
            assert collected

        stream_agent = llm.create_llm_agent(
            settings,
            model=TestModel(custom_output_text="hi"),
            call_site="unit.s",
        )
        with pytest.raises(_CallerStreamError, match="consumer aborted"):
            async with stream_agent.model.request_stream([], None, ModelRequestParameters()):
                raise _CallerStreamError("consumer aborted")

        calls: list[str] = []

        def _over_limit(_msgs: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            calls.append("primary")
            return ModelResponse(
                parts=[TextPart("ok")], usage=RequestUsage(input_tokens=50, output_tokens=500)
            )

        lim_agent = llm.create_llm_agent(
            settings,
            model=FunctionModel(_over_limit),
            fallback_models=[FunctionModel(lambda _m, _i: ModelResponse(parts=[TextPart("fb")]))],
            call_site="unit.limit",
            max_attempts=3,
            usage_limits=UsageLimits(
                request_limit=1, output_tokens_limit=10, cost_limit=Decimal("0.05")
            ),
        )
        with pytest.raises(UsageLimitExceeded):
            await lim_agent.run("go")
        assert calls == ["primary"]

    asyncio.run(_async_paths())

    with agent.run_stream_sync("ping") as sync_stream:
        assert sync_stream.get_output().answer == "ok"

    for prov, model_id in (
        ("zai", "glm-5.3-flash"),
        ("openai", "gpt-4o-mini"),
        ("openrouter", "openai/gpt-4o-mini"),
    ):
        prov_agent = llm.create_llm_agent(
            llm.LlmSettings(provider=prov, api_key="test-key", small_model=model_id),
            call_site="unit.prov",
            include_stream_usage=False,
        )
        assert prov_agent.model.wrapped.client.max_retries == 0

    def _fail(_msgs: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="m", body="secret-token")

    fb_agent = llm.create_llm_agent(
        settings,
        model=FunctionModel(_fail),
        fallback_models=[
            FunctionModel(lambda _m, _i: ModelResponse(parts=[TextPart('{"answer": "fallback"}')]))
        ],
        output_type=_Out,
        call_site="unit.fallback",
        max_attempts=1,
    )
    assert fb_agent.run_sync("go").output == _Out(answer="fallback")

    anon_agent = llm.create_llm_agent(settings, model=TestModel(custom_output_text="hi"))
    with caplog.at_level(logging.WARNING, logger="infra2_sdk.llm"):
        assert anon_agent.run_sync("hi").output == "hi"
    assert any("missing explicit call_site" in rec.message for rec in caplog.records)


def test_stream_text_zai_extra_body_retryable_and_midstream_redaction() -> None:
    dec = llm.DecodingSpec(decoding_version="stream.v1", max_tokens=256)
    cfg = llm.LlmSettings(provider="zai", api_key="test-key", small_model="glm-5.3-flash")
    zai_agent = llm.create_llm_agent(cfg, decoding=dec, call_site="unit.zai")
    assert zai_agent.model.wrapped.settings["extra_body"]["max_tokens"] == 256

    assert llm.is_retryable_llm_error(ModelHTTPError(429, "m", body="rate limited")) is True
    assert llm.is_retryable_llm_error(ModelHTTPError(500, "m", body="server error")) is True
    assert llm.is_retryable_llm_error(ModelHTTPError(529, "m", body="overloaded")) is True
    assert (
        llm.is_retryable_llm_error(
            ModelHTTPError(429, "m", body={"error": {"code": "1113", "message": "arrears"}})
        )
        is False
    )
    assert (
        llm.is_retryable_llm_error(
            ModelHTTPError(429, "m", provider_error_code="1310", body="quota exhausted")
        )
        is False
    )
    from pydantic_ai.exceptions import (
        ModelRateLimitError,
        ModelServerError,
        ModelTimeoutError,
        ModelUnavailableError,
    )

    assert llm.is_retryable_llm_error(ModelServerError(500, "m", body="upstream")) is True
    assert llm.is_retryable_llm_error(ModelUnavailableError(503, "m", body="busy")) is True
    assert llm.is_retryable_llm_error(ModelTimeoutError(408, "m", body="slow")) is True
    assert (
        llm.is_retryable_llm_error(ModelRateLimitError(429, "m", body={"error": {"code": "1113"}}))
        is False
    )
    assert llm.is_retryable_llm_error(ModelHTTPError(400, "m", body='{"code": "1302"}')) is True
    assert (
        llm.is_retryable_llm_error(
            ModelHTTPError(400, "m", body={"error": {"code": "1305", "message": "busy"}})
        )
        is True
    )
    assert llm.is_retryable_llm_error(ModelHTTPError(400, "m", body="bad input")) is False
    assert llm.is_retryable_llm_error(ValueError("bad")) is False

    # create_llm_agent wires is_retryable_llm_error by default (terminal 429 fails fast).
    async def _run_default_retry_wiring() -> None:
        calls = 0

        def _timeout_then_ok(_msgs: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise ModelRequestTimeout("m", 1.0)
            return ModelResponse(parts=[TextPart("recovered")])

        retrying = llm.create_llm_agent(
            cfg,
            model=FunctionModel(_timeout_then_ok),
            call_site="unit.retry_timeout",
            max_attempts=2,
            initial_delay=0.01,
        )
        assert (await retrying.run("ping")).output == "recovered"
        assert calls == 2

        term_calls = 0

        def _terminal_429(_msgs: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
            nonlocal term_calls
            term_calls += 1
            raise ModelHTTPError(
                429, "m", body={"error": {"code": "1113", "message": "insufficient balance"}}
            )

        fast_fail = llm.create_llm_agent(
            cfg,
            model=FunctionModel(_terminal_429),
            call_site="unit.terminal_429",
            max_attempts=3,
            initial_delay=0.01,
        )
        with pytest.raises(ModelHTTPError):
            await fast_fail.run("ping")
        assert term_calls == 1

    import asyncio

    asyncio.run(_run_default_retry_wiring())

    async def _run_stream_checks() -> None:
        # Upstream HTTP fault occurring while caller consumes stream must still be scrubbed.
        class _FailMidStreamModel(TestModel):
            @contextlib.asynccontextmanager  # type: ignore[misc]
            async def request_stream(self, *args, **kwargs):
                class _BadStream:
                    model_name = "glm-5.3-flash"

                    async def __aiter__(self):
                        raise ModelHTTPError(
                            401,
                            "glm-5.3-flash",
                            body={"error": {"message": "Bearer sk-midstream-999"}},
                        )
                        yield  # pragma: no cover

                yield _BadStream()

        gm = llm.create_llm_agent(
            cfg, model=_FailMidStreamModel(), call_site="unit.midstream"
        ).model
        with pytest.raises(ModelHTTPError) as exc_info:
            async with gm.request_stream([], None, ModelRequestParameters()) as s:
                async for _ in s:
                    pass
        assert "sk-midstream-999" not in str(exc_info.value)

        # Bounded stream_text yields chunks, records usage, and supports early aclose cancellation.
        agent = llm.create_llm_agent(
            cfg,
            model=TestModel(custom_output_text="alpha beta gamma"),
            call_site="unit.stream_text",
            decoding=dec,
        )
        usage_out: dict[str, int] = {}
        chunks = [
            chunk
            async for chunk in llm.stream_text(
                agent, "hello", call_site="unit.stream_text", usage_out=usage_out
            )
        ]
        assert "".join(chunks) == "alpha beta gamma"
        assert usage_out["output_tokens"] > 0

        cancelled = False

        class _SlowStreamAgent:
            @contextlib.asynccontextmanager
            async def run_stream(self, _prompt: str, **_kw):
                class _Res:
                    usage = RequestUsage()

                    async def stream_text(self, *, delta: bool = True):
                        try:
                            yield "first-"
                            await asyncio.sleep(10)
                            yield "never"
                        except asyncio.CancelledError:
                            nonlocal cancelled
                            cancelled = True
                            raise

                yield _Res()

        it = llm.stream_text(_SlowStreamAgent(), "hi")
        assert await anext(it) == "first-"
        await it.aclose()
        assert cancelled is True

    asyncio.run(_run_stream_checks())
