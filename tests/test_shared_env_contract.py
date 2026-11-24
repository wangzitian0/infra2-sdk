"""Tests for the converged cross-repository shared environment contract (#227)."""

from __future__ import annotations

import pytest

from infra2_sdk.runtime import (
    LlmSettings,
    assert_distinct_release_identity,
    check_shared_env_conformance,
    shared_env_contract,
)


def test_shared_env_contract_covers_converged_cross_repo_groups() -> None:
    contract = shared_env_contract()
    groups = contract["groups"]
    assert set(groups) >= {"runtime", "postgres_redis", "s3", "llm", "frontend_telemetry"}
    llm_names = {entry["name"]: entry for entry in groups["llm"]}
    # #236 Release B: the LLM aliases are retired, not read.
    assert llm_names["LLM_API_KEY"]["deprecated_aliases"] == []
    retired = {entry["name"]: entry["use"] for entry in contract["retired"]}
    assert retired["AI_API_KEY"] == retired["ZAI_API_KEY"] == "LLM_API_KEY"
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
            "DATA_ENGINE_IMAGE_DIGEST",
            "TRUEALPHA_CONFIGURATION_SHA256",
            "TRUEALPHA_RELEASE_MANIFEST_ID",
        ]
    )
    assert findings == [
        "AI_API_KEY is deprecated: use LLM_API_KEY",
        "APP_ENV is deprecated: use ENVIRONMENT",
        "CANARY_POSTGRES_USER is deprecated: use DATABASE_URL",
        "CANARY_S3_BUCKET is deprecated: use S3_BUCKET",
        "DATA_ENGINE_IMAGE_DIGEST is deprecated: use IMAGE_DIGEST",
        "SMALL_MODEL is deprecated: use LLM_SMALL_MODEL",
        "TRUEALPHA_CONFIGURATION_SHA256 is deprecated: use CONFIGURATION_SHA256",
        "TRUEALPHA_RELEASE_MANIFEST_ID is deprecated: use RELEASE_ID",
        "ZAI_API_KEY is deprecated: use LLM_API_KEY",
    ]
    retired = {entry["name"]: entry["use"] for entry in shared_env_contract()["retired"]}
    assert retired["CANARY_S3_BUCKET"] == "S3_BUCKET"
    assert retired["TRUEALPHA_RELEASE_MANIFEST_ID"] == "RELEASE_ID"
    assert retired["TRUEALPHA_CONFIGURATION_SHA256"] == "CONFIGURATION_SHA256"
    assert retired["DATA_ENGINE_IMAGE_DIGEST"] == "IMAGE_DIGEST"


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
        }
    )
    assert cfg.provider == "openai-compatible"
    assert cfg.base_url == "https://api.example.com/v1"
    assert cfg.api_key == "tok-new"
    assert cfg.large_model == "model-l"
    assert cfg.small_model == "model-s"
    assert cfg.multimodal_model == "model-mm"

    # #236 Release B: a retired name is not read, so it cannot shadow or conflict.
    legacy = LlmSettings.from_mapping(
        {"ZAI_API_KEY": "tok-old", "AI_PROVIDER": "other", "SMALL_MODEL": "legacy-small"}
    )
    assert legacy.api_key is None
    assert legacy.provider == "zai"
    assert legacy.small_model == "glm-5.3-flash"
    both = LlmSettings.from_mapping({"LLM_API_KEY": "tok-1", "AI_API_KEY": "tok-2"})
    assert both.api_key == "tok-1"
