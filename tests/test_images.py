from __future__ import annotations

import pytest

from infra2_sdk.images import (
    get_platform_image,
    get_platform_image_spec,
    load_platform_images,
)


def test_load_platform_images() -> None:
    catalog = load_platform_images()
    assert "postgres" in catalog
    assert "redis" in catalog
    assert "minio" in catalog
    assert "vault_agent" in catalog


def test_get_platform_image() -> None:
    postgres = get_platform_image("postgres")
    assert postgres == "postgres:16-alpine"

    vault = get_platform_image("vault_agent")
    assert vault == "hashicorp/vault:1.15"

    with pytest.raises(KeyError, match="unknown platform image"):
        get_platform_image("nonexistent_service")


def test_get_platform_image_spec() -> None:
    spec = get_platform_image_spec("minio")
    assert "image" in spec
    assert "recommended_mem_limit" in spec
    assert spec["recommended_mem_limit"] == "512m"
