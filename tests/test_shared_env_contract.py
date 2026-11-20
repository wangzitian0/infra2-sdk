"""Tests for the converged cross-repository shared environment contract (#227)."""

from __future__ import annotations

import pytest

from infra2_sdk.runtime import (
    LlmSettings,
    assert_distinct_release_identity,
    check_shared_env_conformance,
    shared_env_contract,
)
from infra2_sdk.runtime.environment import EnvironmentConflictError


def test_shared_env_contract_covers_converged_cross_repo_groups() -> None:
    contract = shared_env_contract()
    groups = contract["groups"]
    assert set(groups) >= {"runtime", "postgres_redis", "s3", "llm", "frontend_telemetry"}
    llm_names = {entry["name"]: entry for entry in groups["llm"]}
    assert "AI_API_KEY" in llm_names["LLM_API_KEY"]["deprecated_aliases"]
    assert "ZAI_API_KEY" in llm_names["LLM_API_KEY"]["deprecated_aliases"]
    assert llm_names["LLM_API_KEY"]["sensitive"] is True


def test_check_shared_env_conformance_flags_deprecated_names_and_accepts_canonical() -> None:
    assert (
        check_shared_env_conformance(
            [
                "ENVIRONMENT",
                "DATABASE_URL",
                "S3_BUCKET",
                "LLM_API_KEY",
                "NEXT_PUBLIC_APP_URL",
                "OPENPANEL_CLIENT_ID",
            ]
        )
        == []
    )

    findings = check_shared_env_conformance(
        [
            "APP_ENV",
            "AI_API_KEY",
            "ZAI_API_KEY",
            "SMALL_MODEL",
            "CANARY_S3_BUCKET",
            "CANARY_POSTGRES_USER",
            "TRUEALPHA_RELEASE_MANIFEST_ID",
        ]
    )
    assert findings == [
        "AI_API_KEY is deprecated: use LLM_API_KEY",
        "APP_ENV is deprecated: use ENVIRONMENT",
        "CANARY_POSTGRES_USER is deprecated: use DATABASE_URL",
        "CANARY_S3_BUCKET is deprecated: use S3_BUCKET",
        "SMALL_MODEL is deprecated: use LLM_SMALL_MODEL",
        "TRUEALPHA_RELEASE_MANIFEST_ID is deprecated: use RELEASE_ID",
        "ZAI_API_KEY is deprecated: use LLM_API_KEY",
    ]


def test_assert_distinct_release_identity_rejects_frozen_staging_prod_collision() -> None:
    with pytest.raises(ValueError, match="duplicate RELEASE_ID"):
        assert_distinct_release_identity(
            [
                {
                    "environment": "staging",
                    "release_id": "release-manifest:aaa",
                    "git_commit_sha": "v0.2.101",
                    "image_digest": "sha256:111",
                },
                {
                    "environment": "production",
                    "release_id": "release-manifest:aaa",
                    "git_commit_sha": "v0.2.99",
                    "image_digest": "sha256:222",
                },
            ]
        )

    assert_distinct_release_identity(
        [
            {
                "environment": "staging",
                "release_id": "release-manifest:bbb",
                "git_commit_sha": "v0.2.101",
                "image_digest": "sha256:111",
            },
            {
                "environment": "production",
                "release_id": "release-manifest:aaa",
                "git_commit_sha": "v0.2.99",
                "image_digest": "sha256:222",
            },
        ]
    )

    # Identical builds (e.g. just-promoted staging == production) are valid.
    assert_distinct_release_identity(
        [
            {
                "environment": "staging",
                "release_id": "release-manifest:aaa",
                "git_commit_sha": "v0.2.101",
                "image_digest": "sha256:111",
            },
            {
                "environment": "production",
                "release_id": "release-manifest:aaa",
                "git_commit_sha": "v0.2.101",
                "image_digest": "sha256:111",
            },
        ]
    )


def test_llm_settings_from_mapping_prefers_canonical_and_rejects_conflicts() -> None:
    cfg = LlmSettings.from_mapping(
        {
            "LLM_PROVIDER": "openai-compatible",
            "LLM_BASE_URL": "https://api.example.com/v1",
            "LLM_API_KEY": "tok-new",
            "LLM_LARGE_MODEL": "model-l",
            "LLM_SMALL_MODEL": "model-s",
            "LLM_MULTIMODAL_MODEL": "model-mm",
            "LLM_ENCRYPTION_KEYS": "k1, k2",
        }
    )
    assert cfg.provider == "openai-compatible"
    assert cfg.base_url == "https://api.example.com/v1"
    assert cfg.api_key == "tok-new"
    assert cfg.large_model == "model-l"
    assert cfg.small_model == "model-s"
    assert cfg.multimodal_model == "model-mm"
    assert cfg.encryption_keys == ("k1", "k2")

    legacy = LlmSettings.from_mapping(
        {"ZAI_API_KEY": "tok-old", "AI_PROVIDER": "zai", "SMALL_MODEL": "legacy-small"}
    )
    assert legacy.api_key == "tok-old"
    assert legacy.provider == "zai"
    assert legacy.small_model == "legacy-small"

    with pytest.raises(EnvironmentConflictError, match="LLM_API_KEY"):
        LlmSettings.from_mapping({"LLM_API_KEY": "tok-1", "AI_API_KEY": "tok-2"})
