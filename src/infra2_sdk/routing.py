"""Canonical domain and routing SSOT."""

from __future__ import annotations

import re
from dataclasses import dataclass

from infra2_sdk.runtime.environment import (
    _CANARY_SLOT as CANARY_SLOT,
)
from infra2_sdk.runtime.environment import (
    EnvironmentTier,
    _canonicalize_preview_alias,
    is_preview_alias,
    to_environment_tier,
)

_DEFAULT_BASE_DOMAIN = "zitian.party"
_SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]*\Z")


def resolve_app_hostname(
    slug: str,
    tier: EnvironmentTier | str,
    *,
    base_domain: str = _DEFAULT_BASE_DOMAIN,
    slot_id: str | int | None = None,
    custom_prod_domain: str | None = None,
    is_canary: bool = False,
) -> str:
    slug = str(slug).strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError(f"invalid slug {slug!r}")

    raw_tier = str(tier).strip().lower() if not isinstance(tier, EnvironmentTier) else tier.value
    if is_canary or raw_tier in ("canary", "deploy-v2-canary"):
        return f"{slug}-{CANARY_SLOT}.{base_domain}"
    if slot_id is not None and str(slot_id).strip().lower() == CANARY_SLOT:
        return f"{slug}-{CANARY_SLOT}.{base_domain}"
    if is_preview_alias(raw_tier):
        return f"{slug}-{_canonicalize_preview_alias(raw_tier)}.{base_domain}"

    canonical_tier = to_environment_tier(tier)
    if canonical_tier is EnvironmentTier.PRODUCTION:
        return custom_prod_domain.strip().lower() if custom_prod_domain else f"{slug}.{base_domain}"
    if canonical_tier is EnvironmentTier.STAGING:
        return f"{slug}-staging.{base_domain}"
    if canonical_tier is EnvironmentTier.PREVIEW:
        if slot_id is not None:
            clean_slot = (
                f"pr-{slot_id}" if isinstance(slot_id, int) else str(slot_id).strip().lower()
            )
            if is_preview_alias(clean_slot):
                return f"{slug}-{_canonicalize_preview_alias(clean_slot)}.{base_domain}"
            return f"{slug}-preview-{clean_slot}.{base_domain}"
        return f"{slug}-preview.{base_domain}"
    return f"{slug}-{canonical_tier.value.replace('_', '-')}.{base_domain}"


def resolve_service_url(
    slug: str,
    tier: EnvironmentTier | str,
    *,
    base_domain: str = _DEFAULT_BASE_DOMAIN,
    path: str = "/",
    slot_id: str | int | None = None,
    is_canary: bool = False,
    scheme: str = "https",
) -> str:
    host = resolve_app_hostname(
        slug, tier, base_domain=base_domain, slot_id=slot_id, is_canary=is_canary
    )
    return f"{scheme}://{host}{path if path.startswith('/') else f'/{path}'}"


@dataclass(frozen=True)
class PreviewStackCoordinates:
    slug: str
    tier: EnvironmentTier
    deployment_environment: str
    env_suffix: str
    env_domain_suffix: str
    app_host: str
    preview_url: str
    cpu_shares: int
    network_suffix: str
    ephemeral_database_url: str


def derive_preview_coordinates(
    slug: str,
    tier: EnvironmentTier | str,
    *,
    base_domain: str = _DEFAULT_BASE_DOMAIN,
    slot_id: str | int | None = None,
    custom_prod_domain: str | None = None,
    is_canary: bool = False,
    scheme: str = "https",
) -> PreviewStackCoordinates:
    app_host = resolve_app_hostname(
        slug,
        tier,
        base_domain=base_domain,
        slot_id=slot_id,
        custom_prod_domain=custom_prod_domain,
        is_canary=is_canary,
    )
    raw_tier = str(tier).strip().lower() if not isinstance(tier, EnvironmentTier) else tier.value
    slot_str = str(slot_id).strip().lower() if slot_id is not None else None

    if is_canary or raw_tier in ("canary", "deploy-v2-canary") or slot_str == CANARY_SLOT:
        canonical_tier, deployment_environment, suffix, cpu_shares = (
            EnvironmentTier.PREVIEW,
            CANARY_SLOT,
            f"-{CANARY_SLOT}",
            256,
        )
    else:
        canonical_tier = to_environment_tier(tier)
        if canonical_tier is EnvironmentTier.PRODUCTION:
            deployment_environment, suffix, cpu_shares = "production", "", 1024
        elif canonical_tier is EnvironmentTier.STAGING:
            deployment_environment, suffix, cpu_shares = "staging", "-staging", 512
        else:
            if is_preview_alias(raw_tier):
                slot_label = _canonicalize_preview_alias(raw_tier)
            elif isinstance(slot_id, int):
                slot_label = f"pr-{slot_id}"
            elif slot_str and is_preview_alias(slot_str):
                slot_label = _canonicalize_preview_alias(slot_str)
            elif slot_str:
                slot_label = f"preview-{slot_str}"
            else:
                slot_label = "preview"
            deployment_environment, suffix, cpu_shares = slot_label, f"-{slot_label}", 256

    db_name = re.sub(r"[^a-z0-9_]", "_", slug.lower()) + "_preview"
    return PreviewStackCoordinates(
        slug=slug,
        tier=canonical_tier,
        deployment_environment=deployment_environment,
        env_suffix=suffix,
        env_domain_suffix=suffix,
        app_host=app_host,
        preview_url=f"{scheme}://{app_host}",
        cpu_shares=cpu_shares,
        network_suffix=suffix,
        ephemeral_database_url=f"postgresql://ephemeral:ephemeral@postgres:5432/{db_name}",
    )


__all__ = [
    "PreviewStackCoordinates",
    "derive_preview_coordinates",
    "resolve_app_hostname",
    "resolve_service_url",
]
