"""Tests for infra2_sdk.llm."""

from __future__ import annotations

import asyncio
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

    assert llm.prompt_sha256(b"\x80\x81") != llm.prompt_sha256(b"\x82\x83")
    s_hash = llm.schema_sha256(_Out)
    r_hash = llm.request_sha256(model="glm-5.3-flash", prompt="hello", schema=s_hash, decoding=dec)
    assert len(s_hash) == len(r_hash) == 64


def test_redact_errors_passthrough_streaming_and_vcr(caplog: pytest.LogCaptureFixture) -> None:
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

    # Control-flow exceptions (ModelRetry, UnexpectedModelBehavior, stream faults) pass through.
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

        # Caller exception inside run_stream must not be overwritten into RuntimeError.
        gm = llm.create_llm_model(
            settings, model=TestModel(custom_output_text="hi"), call_site="unit.s", instrument=False
        )
        with pytest.raises(_CallerStreamError, match="consumer aborted"):
            async with gm.request_stream([], None, ModelRequestParameters()):
                raise _CallerStreamError("consumer aborted")

        # UsageLimitExceeded propagates without triggering retries or fallback.
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
        m = llm.create_llm_model(
            llm.LlmSettings(provider=prov, api_key="test-key", small_model=model_id),
            call_site="unit.prov",
            instrument=False,
            include_stream_usage=False,
        )
        assert m.wrapped.client.max_retries == 0

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

    inst_wrapped = llm.create_llm_model(
        settings,
        model=TestModel(custom_output_text="wrapped"),
        fallback_models=[TestModel(custom_output_text="wrapped-fb")],
        call_site="unit.wrapped",
        instrument=True,
    )
    rewrapped = llm.create_llm_model(settings, model=inst_wrapped, call_site="unit.rewrapped")
    assert isinstance(rewrapped.wrapped.models[0], llm.GuardedLlmModel)

    anon_agent = llm.create_llm_agent(settings, model=TestModel(custom_output_text="hi"))
    with caplog.at_level(logging.WARNING, logger="infra2_sdk.llm"):
        assert anon_agent.run_sync("hi").output == "hi"
    assert any("missing explicit call_site" in rec.message for rec in caplog.records)

    vcfg = llm.vcr_llm_config(cassette_library_dir="/tmp/cassettes", record_mode="none")
    req_obj = type(
        "R",
        (),
        {
            "uri": "https://api.example?api_key=abc",
            "headers": {"Authorization": ["Bearer s"], "Accept": ["application/json"]},
        },
    )()
    scrubbed_req = vcfg["before_record_request"](req_obj)
    assert "abc" not in scrubbed_req.uri
    assert scrubbed_req.headers["Authorization"] == ["REDACTED"]
    assert scrubbed_req.headers["Accept"] == ["application/json"]
    scrubbed_resp = vcfg["before_record_response"](
        {"headers": {"Set-Cookie": ["s=1"]}, "body": {"string": '{"err":"Bearer sk-leak999999"}'}}
    )
    assert scrubbed_resp["headers"]["Set-Cookie"] == ["REDACTED"]
    assert "sk-leak999999" not in scrubbed_resp["body"]["string"]

    with llm.allow_vcr_model_requests():
        from pydantic_ai import models

        assert models.ALLOW_MODEL_REQUESTS is True
