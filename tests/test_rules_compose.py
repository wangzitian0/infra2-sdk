from __future__ import annotations

from infra2_sdk.rules.compose import (
    find_bare_latest_violations,
    inspect_service_resource_limits,
    is_memory_ceiling,
    tag_of_image_ref,
)


def test_is_memory_ceiling_values() -> None:
    assert not is_memory_ceiling(None)
    assert not is_memory_ceiling(False)
    assert not is_memory_ceiling(True)
    assert not is_memory_ceiling(0)
    assert not is_memory_ceiling(-100)
    assert not is_memory_ceiling("0")
    assert not is_memory_ceiling("0b")
    assert not is_memory_ceiling("not-a-number")
    assert not is_memory_ceiling([])

    assert is_memory_ceiling(1024)
    assert is_memory_ceiling(1.5)
    assert is_memory_ceiling("512m")
    assert is_memory_ceiling("512M")
    assert is_memory_ceiling("1.5g")
    assert is_memory_ceiling("2G")
    assert is_memory_ceiling("1073741824")
    assert is_memory_ceiling("64k")
    assert is_memory_ceiling("1t")


def test_tag_of_image_ref() -> None:
    assert tag_of_image_ref("postgres:16-alpine") == "16-alpine"
    assert tag_of_image_ref("registry.example.com:5000/app:v1.0") == "v1.0"
    assert tag_of_image_ref("redis") == ""
    assert tag_of_image_ref("registry.example.com:5000/redis") == ""


def test_find_bare_latest_violations() -> None:
    compose = """
services:
  app:
    image: myrepo/app:latest
  db:
    image: postgres:16-alpine
  cache:
    image: redis:latest
  safe1:
    image: myrepo/app:latest@sha256:abcdef1234567890
  safe2:
    image: myrepo/worker:${IMAGE_TAG}
"""
    violations = find_bare_latest_violations(compose)
    assert violations == ["myrepo/app:latest", "redis:latest"]


def test_inspect_service_resource_limits() -> None:
    services = {
        "web": {"image": "app", "mem_limit": "512m"},
        "worker": {"image": "worker", "mem_limit": "1g"},
        "db": {"image": "postgres", "mem_limit": 0},
        "cache": {"image": "redis"},
        "broken": "not a dict",
    }
    compliant, non_compliant = inspect_service_resource_limits(services)
    assert compliant == ["web", "worker"]
    assert non_compliant == ["broken", "cache", "db"]
