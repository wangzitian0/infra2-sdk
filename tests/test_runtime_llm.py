"""Tests for LlmSettings runtime configuration contracts."""

from __future__ import annotations

from dataclasses import fields

import pytest

from infra2_sdk.runtime.llm import LlmSettings


def test_llm_settings_defaults() -> None:
    settings = LlmSettings()
    assert settings.provider == "zai"
    assert settings.base_url == "https://api.z.ai/api/coding/paas/v4"
    assert settings.api_key is None


def test_llm_settings_repr_hides_the_api_key() -> None:
    assert "secret-key" not in repr(LlmSettings(api_key="secret-key"))


@pytest.mark.parametrize(
    "removed",
    [
        "chat_completions_path",
        "json_timeout_seconds",
        "json_max_tokens",
        "json_disable_thinking",
        "json_seed",
        "extract_max_attempts",
        "encryption_keys",
    ],
)
def test_the_release_b_fields_are_gone(removed: str) -> None:
    """#236 Release B: the LlmClient-only fields left with LlmClient."""
    assert removed not in {item.name for item in fields(LlmSettings)}
    with pytest.raises(TypeError, match=removed):
        LlmSettings(**{removed: "x"})


def test_llm_settings_validation_errors() -> None:
    with pytest.raises(ValueError, match="provider must be non-empty"):
        LlmSettings(provider="  ")

    with pytest.raises(ValueError, match="base_url must use http:// or https:// with a host"):
        LlmSettings(base_url="ftp://invalid.com")

    with pytest.raises(ValueError, match="base_url must not contain credentials"):
        LlmSettings(base_url="https://user:pass@open.bigmodel.cn/api/paas/v4")


def test_llm_settings_model_roles() -> None:
    settings = LlmSettings()
    assert settings.role_model("large") == settings.large_model == "glm-5.3-flash"
    assert settings.role_model("small") == settings.small_model == "glm-5.3-flash"
    assert settings.role_model("multimodal") == settings.multimodal_model == "glm-5.3-flash"
    assert settings.role_reasoning_effort("large") == "high"
    assert settings.role_reasoning_effort("small") == "low"
    assert settings.role_reasoning_effort("multimodal") == "low"


def test_llm_settings_rejects_an_unknown_role_and_an_empty_role() -> None:
    with pytest.raises(ValueError, match="unknown model role"):
        LlmSettings().role_model("medium")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unknown model role"):
        LlmSettings().role_reasoning_effort("medium")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="large_reasoning_effort must be non-empty"):
        LlmSettings(large_reasoning_effort="")
    with pytest.raises(ValueError, match="large_model must be non-empty"):
        LlmSettings(large_model=" ")


_FIELDS_BEFORE_ROLES = ["provider", "base_url", "api_key"]


def test_llm_settings_positional_order_is_unchanged() -> None:
    names = [item.name for item in fields(LlmSettings)]
    assert names[: len(_FIELDS_BEFORE_ROLES)] == _FIELDS_BEFORE_ROLES
    settings = LlmSettings("zai", "https://api.z.ai/api/coding/paas/v4", "k")
    assert settings.api_key == "k"
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
        settings.large_model,
        settings.small_model,
        settings.multimodal_model,
    }
    assert defaults, "the guard must see the default models"
    assert defaults <= CODING_PLAN_MODELS, sorted(defaults - CODING_PLAN_MODELS)
