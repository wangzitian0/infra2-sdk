"""Tests for LlmSettings runtime configuration contracts."""

from __future__ import annotations

from dataclasses import fields, replace

import pytest

from infra2_sdk.runtime.llm import LlmSettings


def test_llm_settings_defaults() -> None:
    settings = LlmSettings()
    assert settings.provider == "zai"
    assert settings.base_url == "https://api.z.ai/api/coding/paas/v4"
    assert settings.api_key is None
    assert settings.primary_model == "glm-5.3-flash"
    assert settings.ocr_model == "glm-5.3-flash"
    assert settings.vision_model == "glm-5.3-flash"
    assert settings.fallback_models == ()
    assert settings.vision_fallback_models == ()
    assert settings.chat_completions_path == "/chat/completions"
    assert settings.layout_parsing_path == "/layout_parsing"
    assert settings.json_timeout_seconds == 360.0
    assert settings.json_max_tokens == 8192
    assert settings.json_disable_thinking is True
    assert settings.json_seed is None
    assert settings.extract_max_attempts == 2
    assert settings.encryption_keys == ()


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


def test_llm_settings_model_roles_and_derived_slots() -> None:
    settings = LlmSettings()
    assert settings.role_model("large") == settings.large_model == "glm-5.3-flash"
    assert settings.role_model("small") == settings.small_model == "glm-5.3-flash"
    assert settings.role_model("multimodal") == settings.multimodal_model == "glm-5.3-flash"
    assert settings.role_reasoning_effort("large") == "high"
    assert settings.role_reasoning_effort("small") == "low"
    assert settings.role_reasoning_effort("multimodal") == "low"
    assert settings.primary_model == settings.small_model
    assert settings.ocr_model == settings.multimodal_model
    assert settings.vision_model == settings.multimodal_model


def test_llm_settings_slots_follow_a_changed_role() -> None:
    settings = LlmSettings(small_model="small-x", multimodal_model="vision-x")
    assert settings.primary_model == "small-x"
    assert settings.ocr_model == "vision-x"
    assert settings.vision_model == "vision-x"


def test_llm_settings_explicit_slots_win_over_roles() -> None:
    settings = LlmSettings(primary_model="custom", vision_model=None, ocr_model="ocr-x")
    assert settings.primary_model == "custom"
    assert settings.vision_model is None
    assert settings.ocr_model == "ocr-x"


def test_llm_settings_rejects_an_unknown_role_and_an_empty_role() -> None:
    with pytest.raises(ValueError, match="unknown model role"):
        LlmSettings().role_model("medium")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown model role"):
        LlmSettings().role_reasoning_effort("medium")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="large_reasoning_effort must be non-empty"):
        LlmSettings(large_reasoning_effort="")
    with pytest.raises(ValueError, match="large_model must be non-empty"):
        LlmSettings(large_model=" ")


_FIELDS_BEFORE_ROLES = [
    "provider",
    "base_url",
    "api_key",
    "primary_model",
    "ocr_model",
    "vision_model",
    "fallback_models",
    "vision_fallback_models",
    "chat_completions_path",
    "layout_parsing_path",
    "json_timeout_seconds",
    "json_max_tokens",
    "json_disable_thinking",
    "json_seed",
    "extract_max_attempts",
    "encryption_keys",
]


def test_llm_settings_positional_order_is_unchanged() -> None:
    names = [item.name for item in fields(LlmSettings)]
    assert names[: len(_FIELDS_BEFORE_ROLES)] == _FIELDS_BEFORE_ROLES
    settings = LlmSettings("zai", "https://api.z.ai/api/coding/paas/v4", None, "positional-primary")
    assert settings.primary_model == "positional-primary"
    assert settings.small_model == "glm-5.3-flash"


@pytest.mark.parametrize(
    "role_field",
    [
        "large_model",
        "small_model",
        "multimodal_model",
        "large_reasoning_effort",
        "small_reasoning_effort",
        "multimodal_reasoning_effort",
    ],
)
@pytest.mark.parametrize("bad_value", ["", "  ", None])
def test_llm_settings_rejects_an_empty_or_missing_role_value(
    role_field: str, bad_value: object
) -> None:
    with pytest.raises(ValueError, match=f"{role_field} must be non-empty"):
        LlmSettings(**{role_field: bad_value})


def test_llm_settings_explicit_none_ocr_model_is_kept() -> None:
    assert LlmSettings(ocr_model=None).ocr_model is None


def test_llm_settings_replace_does_not_rederive_slots() -> None:
    changed = replace(LlmSettings(), small_model="small-x")
    assert changed.small_model == "small-x"
    assert changed.primary_model == "glm-5.3-flash"
    assert LlmSettings(small_model="small-x").primary_model == "small-x"


def test_llm_settings_each_role_maps_to_its_own_fields() -> None:
    settings = LlmSettings(
        large_model="model-large",
        small_model="model-small",
        multimodal_model="model-multimodal",
        large_reasoning_effort="effort-large",
        small_reasoning_effort="effort-small",
        multimodal_reasoning_effort="effort-multimodal",
    )
    for role in ("large", "small", "multimodal"):
        assert settings.role_model(role) == f"model-{role}"
        assert settings.role_reasoning_effort(role) == f"effort-{role}"


def test_llm_settings_role_fields_are_keyword_only() -> None:
    role_fields = {
        item.name: item.kw_only
        for item in fields(LlmSettings)
        if item.name.endswith(("_model", "_reasoning_effort"))
        and item.name.split("_")[0] in {"large", "small", "multimodal"}
    }
    assert set(role_fields) == {
        "large_model",
        "small_model",
        "multimodal_model",
        "large_reasoning_effort",
        "small_reasoning_effort",
        "multimodal_reasoning_effort",
    }
    assert all(role_fields.values())


# The models that every GLM Coding Plan serves (https://docs.z.ai/devpack/faq: "All plans
# support GLM-5.3, GLM-5.3-Flash"). glm-5.3-flashx is not on the plan: a plan key gets
# Z.ai code 1311 (HTTP 429) on every call (finance_report#2580).
CODING_PLAN_MODELS = {"glm-5.3", "glm-5.3-flash"}


def test_every_default_model_is_served_by_the_coding_plan() -> None:
    settings = LlmSettings()
    defaults = {
        settings.primary_model,
        settings.ocr_model,
        settings.vision_model,
        settings.large_model,
        settings.small_model,
        settings.multimodal_model,
        *settings.fallback_models,
        *settings.vision_fallback_models,
    }
    defaults.discard(None)
    assert defaults, "the guard must see the default models"
    assert defaults <= CODING_PLAN_MODELS, sorted(defaults - CODING_PLAN_MODELS)
