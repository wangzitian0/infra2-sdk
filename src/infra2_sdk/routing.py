"""Canonical domain and routing SSOT."""

from __future__ import annotations

import re
from dataclasses import dataclass

from infra2_sdk.runtime.environment import (
    CANARY_SLOT,
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
    """Compute the canonical FQDN for a service in a given environment tier.

    SSOT Rules:
    1. Canary / Synthetic Probe:
       - Returns ``f"{slug}-canary-preview.{base_domain}"``
    2. Production:
       - If ``custom_prod_domain`` is provided: returns it verbatim (e.g. "truealpha.club")
       - Otherwise: ``f"{slug}.{base_domain}"`` (e.g. "report.zitian.party")
    3. Staging:
       - ``f"{slug}-staging.{base_domain}"`` (e.g. "report-staging.zitian.party")
    4. Preview:
       - Default single slot: ``f"{slug}-preview.{base_domain}"``
       - Preview alias (``branch-*``, ``pr-*``, ``commit-*``, ``tag-v*``, or an
         integer PR number as ``slot_id``): ``f"{slug}-{alias}.{base_domain}"``
       - Other slot: ``f"{slug}-preview-{slot_id}.{base_domain}"``
    """
    slug = str(slug).strip().lower()
    if not _SLUG_RE.match(slug):
        raise ValueError(f"invalid slug {slug!r}")

    # Check for canary
    raw_tier = str(tier).strip().lower() if not isinstance(tier, EnvironmentTier) else tier.value
    if is_canary or raw_tier in ("canary", "deploy-v2-canary"):
        return f"{slug}-{CANARY_SLOT}.{base_domain}"

    # The canary slot id maps to the canary host.
    if slot_id is not None and str(slot_id).strip().lower() == CANARY_SLOT:
        return f"{slug}-{CANARY_SLOT}.{base_domain}"

    # A preview alias in the tier maps to ``{slug}-{alias}``.
    if is_preview_alias(raw_tier):
        return f"{slug}-{_canonicalize_preview_alias(raw_tier)}.{base_domain}"

    canonical_tier = to_environment_tier(tier)

    if canonical_tier is EnvironmentTier.PRODUCTION:
        if custom_prod_domain:
            return custom_prod_domain.strip().lower()
        return f"{slug}.{base_domain}"

    if canonical_tier is EnvironmentTier.STAGING:
        return f"{slug}-staging.{base_domain}"

    if canonical_tier is EnvironmentTier.PREVIEW:
        if slot_id is not None:
            clean_slot = str(slot_id).strip().lower()
            if isinstance(slot_id, int):
                clean_slot = f"pr-{clean_slot}"
            if is_preview_alias(clean_slot):
                return f"{slug}-{_canonicalize_preview_alias(clean_slot)}.{base_domain}"
            return f"{slug}-preview-{clean_slot}.{base_domain}"
        return f"{slug}-preview.{base_domain}"

    # Local / test tiers
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
    """Compute the concrete public URL for a service endpoint."""
    host = resolve_app_hostname(
        slug,
        tier,
        base_domain=base_domain,
        slot_id=slot_id,
        is_canary=is_canary,
    )
    clean_path = path if path.startswith("/") else f"/{path}"
    return f"{scheme}://{host}{clean_path}"


@dataclass(frozen=True)
class PreviewStackCoordinates:
    """Resolved coordinate set for one preview or platform stack."""

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
    """Derive stack coordinates and ephemeral database URL for one service."""
    app_host = resolve_app_hostname(
        slug,
        tier,
        base_domain=base_domain,
        slot_id=slot_id,
        custom_prod_domain=custom_prod_domain,
        is_canary=is_canary,
    )
    preview_url = f"{scheme}://{app_host}"

    raw_tier = str(tier).strip().lower() if not isinstance(tier, EnvironmentTier) else tier.value
    slot_str = str(slot_id).strip().lower() if slot_id is not None else None

    if is_canary or raw_tier in ("canary", "deploy-v2-canary") or slot_str == CANARY_SLOT:
        canonical_tier = EnvironmentTier.PREVIEW
        deployment_environment = CANARY_SLOT
        env_suffix = f"-{CANARY_SLOT}"
        env_domain_suffix = f"-{CANARY_SLOT}"
        cpu_shares = 256
        network_suffix = f"-{CANARY_SLOT}"
    else:
        canonical_tier = to_environment_tier(tier)
        if canonical_tier is EnvironmentTier.PRODUCTION:
            deployment_environment = "production"
            env_suffix = ""
            env_domain_suffix = ""
            cpu_shares = 1024
            network_suffix = ""
        elif canonical_tier is EnvironmentTier.STAGING:
            deployment_environment = "staging"
            env_suffix = "-staging"
            env_domain_suffix = "-staging"
            cpu_shares = 512
            network_suffix = "-staging"
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
            deployment_environment = slot_label
            env_suffix = f"-{slot_label}"
            env_domain_suffix = f"-{slot_label}"
            cpu_shares = 256
            network_suffix = f"-{slot_label}"

    db_name = re.sub(r"[^a-z0-9_]", "_", slug.lower()) + "_preview"
    ephemeral_database_url = f"postgresql://ephemeral:ephemeral@postgres:5432/{db_name}"

    return PreviewStackCoordinates(
        slug=slug,
        tier=canonical_tier,
        deployment_environment=deployment_environment,
        env_suffix=env_suffix,
        env_domain_suffix=env_domain_suffix,
        app_host=app_host,
        preview_url=preview_url,
        cpu_shares=cpu_shares,
        network_suffix=network_suffix,
        ephemeral_database_url=ephemeral_database_url,
    )


__all__ = [
    "PreviewStackCoordinates",
    "derive_preview_coordinates",
    "resolve_app_hostname",
    "resolve_service_url",
]
