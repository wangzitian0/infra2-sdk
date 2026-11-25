"""Tests for infra2_sdk.llm."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest
from pydantic import BaseModel
from pydantic_ai.exceptions import ModelConnectionError, ModelHTTPError, ModelRequestTimeout
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from infra2_sdk import _llm_runtime, llm


class _Out(BaseModel):
    answer: str


def test_llm_settings_decoding_and_digests() -> None:
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
    assert cfg.role_reasoning_effort("large") == "high"
    assert "sk-test-secret" not in repr(cfg)

    with pytest.raises(ValueError, match="unknown model role"):
        cfg.role_model("unknown")  # type: ignore[arg-type]

    dec = llm.DecodingSpec(decoding_version="demo.v1", temperature=0.0, max_tokens=128)
    assert dec.to_model_settings() == {"temperature": 0.0, "max_tokens": 128}
    with pytest.raises(ValueError):
        llm.DecodingSpec(max_tokens=0)
    with pytest.raises(ValueError):
        llm.DecodingSpec(top_p=1.5)

    p_hash = llm.prompt_sha256({"text": ["hello"], "tags": {"a", "b"}})
    s_hash = llm.schema_sha256(_Out)
    r_hash = llm.request_sha256(model="glm-5.3-flash", prompt="hello", schema=s_hash, decoding=dec)
    assert len(p_hash) == len(s_hash) == len(r_hash) == 64

    with pytest.raises(ValueError, match="call_site"):
        _llm_runtime.validate_call_site("INVALID SITE")


def test_redact_llm_error_and_agent_and_vcr(caplog: pytest.LogCaptureFixture) -> None:
    err = ModelHTTPError(
        status_code=401,
        model_name="glm-5.3-flash",
        body={"error": {"code": "bad_auth", "message": "Bearer sk-secret-123"}},
    )
    scrubbed = llm.redact_llm_error(err)
    assert isinstance(scrubbed, ModelHTTPError) and "sk-secret-123" not in str(scrubbed)

    t_err = llm.redact_llm_error(ModelRequestTimeout("m", 5.0, setting="timeout"))
    assert isinstance(t_err, ModelRequestTimeout)
    c_err = llm.redact_llm_error(ModelConnectionError("m", "secret", phase="request"))
    assert isinstance(c_err, ModelConnectionError) and "secret" not in str(c_err)

    generic = llm.redact_llm_error(httpx.ConnectError("Bearer sk-secret-456"))
    assert "sk-secret-456" not in str(generic)

    settings = llm.LlmSettings(provider="zai", api_key="test-key")
    agent = llm.create_llm_agent(
        settings,
        model=TestModel(custom_output_args={"answer": "ok"}),
        output_type=_Out,
        call_site="unit.echo",
        decoding=llm.DecodingSpec(decoding_version="unit.v1"),
    )
    with llm.llm_call_context(call_site="unit.echo", prompt_sha256="a" * 64):
        res = agent.run_sync("ping")
    assert res.output == _Out(answer="ok")

    async def _async_agent_paths() -> None:
        run_r = await agent.run("ping")
        assert run_r.output.answer == "ok"
        async with agent.run_stream("ping") as s:
            assert (await s.get_output()).answer == "ok"
        async with agent.iter("ping") as it:
            async for _node in it:
                pass
            assert it.result is not None

    asyncio.run(_async_agent_paths())

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

    with pytest.raises(ValueError, match="unsupported LLM provider"):
        llm.create_llm_model(
            llm.LlmSettings(provider="unknown_p", api_key="k"),
            instrument=False,
        )

    def _fail(_msgs: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise ModelHTTPError(status_code=503, model_name="m", body="secret-token")

    def _fb(_msgs: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart('{"answer": "fallback"}')])

    fb_agent = llm.create_llm_agent(
        settings,
        model=FunctionModel(_fail),
        fallback_models=[FunctionModel(_fb)],
        output_type=_Out,
        call_site="unit.fallback",
        max_attempts=1,
    )
    assert fb_agent.run_sync("go").output == _Out(answer="fallback")

    anon_agent = llm.create_llm_agent(settings, model=TestModel(custom_output_text="hi"))
    with caplog.at_level(logging.WARNING, logger="infra2_sdk.llm"):
        assert anon_agent.run_sync("hi").output == "hi"
    assert any("missing explicit call_site" in rec.message for rec in caplog.records)

    vcfg = llm.vcr_llm_config(cassette_library_dir="/tmp/cassettes", record_mode="none")
    req_obj = type(
        "R",
        (),
        {"uri": "https://api.example?api_key=abc", "headers": {"Authorization": "Bearer s"}},
    )()
    scrubbed_req = vcfg["before_record_request"](req_obj)
    assert "abc" not in scrubbed_req.uri and scrubbed_req.headers["Authorization"] == "REDACTED"
    scrubbed_resp = vcfg["before_record_response"]({"headers": {"Set-Cookie": ["s=1"]}})
    assert scrubbed_resp["headers"]["Set-Cookie"] == ["REDACTED"]

    with llm.allow_vcr_model_requests():
        from pydantic_ai import models

        assert models.ALLOW_MODEL_REQUESTS is True
