"""SSOT platform image catalog and utilities."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml


def load_platform_images(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load the platform images catalog as ``image_name -> metadata``."""
    source = Path(path) if path is not None else files("infra2_sdk").joinpath("data/platform_images.yaml")
    with source.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    images = data.get("images")
    if not isinstance(images, dict):
        raise ValueError("platform_images.yaml: 'images' must be a mapping")
    return images


def get_platform_image(name: str, *, catalog: dict[str, dict[str, Any]] | None = None) -> str:
    """Return the pinned image reference for a platform service."""
    images = catalog or load_platform_images()
    if name not in images:
        raise KeyError(f"unknown platform image: {name!r}, available: {sorted(images)}")
    return str(images[name]["image"])


def get_platform_image_spec(name: str, *, catalog: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Return the full metadata specification for a platform image."""
    images = catalog or load_platform_images()
    if name not in images:
        raise KeyError(f"unknown platform image: {name!r}, available: {sorted(images)}")
    return dict(images[name])
