"""SSOT platform image catalog and utilities.

.. deprecated:: 2.4.0
    No repository imports this module, and its catalog is not kept in sync with the compose
    files in infra2, which are the source of truth for platform image pins. It is removed in
    3.0.0 (removing a public module needs a major release). Importing it, or calling any of
    its functions, raises a ``DeprecationWarning``.
"""

from __future__ import annotations

import warnings
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

_DEPRECATION_MESSAGE = (
    "infra2_sdk.images is deprecated since 2.4.0 and will be removed in 3.0.0: nothing consumes "
    "it and its catalog is not kept in sync with infra2's compose image pins"
)


def _warn_deprecated(stacklevel: int) -> None:
    warnings.warn(_DEPRECATION_MESSAGE, DeprecationWarning, stacklevel=stacklevel)


_warn_deprecated(stacklevel=3)


def load_platform_images(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load the platform images catalog as ``image_name -> metadata``."""
    _warn_deprecated(stacklevel=3)
    return _load_catalog(path)


def get_platform_image(name: str, *, catalog: dict[str, dict[str, Any]] | None = None) -> str:
    """Return the pinned image reference for a platform service."""
    _warn_deprecated(stacklevel=3)
    images = catalog or _load_catalog()
    if name not in images:
        raise KeyError(f"unknown platform image: {name!r}, available: {sorted(images)}")
    return str(images[name]["image"])


def get_platform_image_spec(
    name: str, *, catalog: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Return the full metadata specification for a platform image."""
    _warn_deprecated(stacklevel=3)
    images = catalog or _load_catalog()
    if name not in images:
        raise KeyError(f"unknown platform image: {name!r}, available: {sorted(images)}")
    return dict(images[name])


def _load_catalog(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    source = (
        Path(path)
        if path is not None
        else files("infra2_sdk").joinpath("data/platform_images.yaml")
    )
    with source.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    images = data.get("images")
    if not isinstance(images, dict):
        raise ValueError("platform_images.yaml: 'images' must be a mapping")
    return images
