"""Tests for LlmSettings runtime configuration contracts."""

from __future__ import annotations

import pytest

from infra2_sdk.runtime.llm import LlmSettings


def test_llm_settings_defaults() -> None:
    settings = LlmSettings()
    assert settings.provider == "zai"
    assert settings.base_url == "https://open.bigmodel.cn/api/paas/v4"
    assert settings.api_key is None
    assert settings.primary_model == "glm-5.3-flash"
    assert settings.ocr_model is None
    assert settings.vision_model is None
    assert settings.fallback_models == ()
    assert settings.vision_fallback_models == ()
    assert settings.chat_completions_path == "/chat/completions"
    assert settings.layout_parsing_path == "/layout_parsing"
    assert settings.json_timeout_seconds == 90.0
    assert settings.json_max_tokens == 16384
    assert settings.json_disable_thinking is True
    assert settings.json_seed == 42
    assert settings.extract_max_attempts == 3
    assert settings.encryption_keys == ()
    assert settings.chat_completions_url == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    assert settings.layout_parsing_url == "https://open.bigmodel.cn/api/paas/v4/layout_parsing"


def test_llm_settings_repr_hides_sensitive_fields() -> None:
    settings = LlmSettings(
        api_key="secret-key",
        encryption_keys=("enc-key-1", "enc-key-2"),
    )
    rendered = repr(settings)
    assert "secret-key" not in rendered
    assert "enc-key-1" not in rendered
    assert "enc-key-2" not in rendered


def test_llm_settings_tuple_coercion() -> None:
    settings = LlmSettings(
        fallback_models="model-a, model-b , ",
        vision_fallback_models=["vision-1", " vision-2 "],
        encryption_keys="key1,key2",
    )
    assert settings.fallback_models == ("model-a", "model-b")
    assert settings.vision_fallback_models == ("vision-1", "vision-2")
    assert settings.encryption_keys == ("key1", "key2")


def test_llm_settings_validation_errors() -> None:
    with pytest.raises(ValueError, match="provider must be non-empty"):
        LlmSettings(provider="  ")

    with pytest.raises(ValueError, match="primary_model must be non-empty"):
        LlmSettings(primary_model="")

    with pytest.raises(ValueError, match="base_url must use http:// or https:// with a host"):
        LlmSettings(base_url="ftp://invalid.com")

    with pytest.raises(ValueError, match="base_url must not contain credentials"):
        LlmSettings(base_url="https://user:pass@open.bigmodel.cn/api/paas/v4")

    with pytest.raises(ValueError, match="chat_completions_path must start with '/'"):
        LlmSettings(chat_completions_path="chat/completions")

    with pytest.raises(ValueError, match="layout_parsing_path must start with '/'"):
        LlmSettings(layout_parsing_path="layout_parsing")

    with pytest.raises(ValueError, match="json_timeout_seconds must be positive"):
        LlmSettings(json_timeout_seconds=0.0)

    with pytest.raises(ValueError, match="json_max_tokens must be at least 1"):
        LlmSettings(json_max_tokens=0)

    with pytest.raises(ValueError, match="extract_max_attempts must be at least 1"):
        LlmSettings(extract_max_attempts=0)


def test_llm_settings_model_chain_deduplication() -> None:
    settings = LlmSettings(
        primary_model="primary-1",
        fallback_models=("fb-1", "primary-1", "fb-2"),
        vision_model="vision-main",
        vision_fallback_models=("vision-fb-1", "vision-main", "vision-fb-2"),
    )
    assert settings.model_chain(vision=False) == ("primary-1", "fb-1", "fb-2")
    assert settings.model_chain(vision=True) == (
        "vision-main",
        "vision-fb-1",
        "vision-fb-2",
    )

    # When vision_model is None, falls back to primary_model for vision chain
    settings_no_vision = LlmSettings(
        primary_model="primary-1",
        vision_fallback_models=("vfb-1", "primary-1"),
    )
    assert settings_no_vision.model_chain(vision=True) == ("primary-1", "vfb-1")
