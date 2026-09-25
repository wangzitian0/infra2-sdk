"""Canonical domain, routing and Dokploy domain specifications SSOT."""

from __future__ import annotations

import re
import warnings
from dataclasses import dataclass
from typing import Any

from infra2_sdk.runtime.environment import EnvironmentTier, to_environment_tier

CANARY_SLOT = "canary-preview"
LEGACY_CANARY_PR = 999
DEFAULT_BASE_DOMAIN = "zitian.party"

_SLUG_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]*\Z")
_HOSTNAME_RE = re.compile(
    r"\A[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*\Z"
)


@dataclass(frozen=True)
class RouteEndpoint:
    """One routing endpoint exposed by a service compose."""

    port: int
    path: str = "/"
    service_name: str | None = None

    def __post_init__(self) -> None:
        if not (1 <= self.port <= 65535):
            raise ValueError(f"port must be between 1 and 65535, got {self.port}")
        if not self.path.startswith("/"):
            raise ValueError(f"path must start with '/', got {self.path!r}")


@dataclass(frozen=True)
class AppRoutePreference:
    """App-level routing preference declared by an application or service."""

    slug: str
    endpoints: tuple[RouteEndpoint, ...] = (RouteEndpoint(port=3000),)
    custom_prod_domain: str | None = None

    def __post_init__(self) -> None:
        if not _SLUG_RE.match(self.slug):
            raise ValueError(f"invalid slug {self.slug!r}: must match {_SLUG_RE.pattern}")
        if not self.endpoints:
            raise ValueError("endpoints must not be empty")
        if self.custom_prod_domain:
            cleaned = self.custom_prod_domain.strip().lower()
            if not _HOSTNAME_RE.match(cleaned):
                raise ValueError(f"invalid custom_prod_domain {self.custom_prod_domain!r}")


@dataclass(frozen=True)
class DokployDomainSpec:
    """A concrete domain specification ready for Dokploy ensure_domains / domain.create."""

    host: str
    port: int
    path: str = "/"
    service_name: str | None = None
    https: bool = True

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "https": self.https,
        }
        if self.service_name:
            result["serviceName"] = self.service_name
        return result


def resolve_app_hostname(
    slug_or_preference: str | AppRoutePreference,
    tier: EnvironmentTier | str,
    *,
    base_domain: str = DEFAULT_BASE_DOMAIN,
    slot_id: str | int | None = None,
    custom_prod_domain: str | None = None,
    is_canary: bool = False,
) -> str:
    """Compute the canonical FQDN for a service in a given environment tier.

    SSOT Rules:
    1. Canary / Synthetic Probe:
       - Always returns ``f"canary-preview.{base_domain}"``
    2. Production:
       - If ``custom_prod_domain`` is provided: returns it verbatim (e.g. "truealpha.club")
       - Otherwise: ``f"{slug}.{base_domain}"`` (e.g. "report.zitian.party")
    3. Staging:
       - ``f"{slug}-staging.{base_domain}"`` (e.g. "report-staging.zitian.party")
    4. Preview:
       - Default single slot: ``f"{slug}-preview.{base_domain}"``
       - Dynamic slot: ``f"{slug}-preview-{slot_id}.{base_domain}"``
    """
    if isinstance(slug_or_preference, AppRoutePreference):
        slug = slug_or_preference.slug
        if custom_prod_domain is None:
            custom_prod_domain = slug_or_preference.custom_prod_domain
    else:
        slug = str(slug_or_preference).strip().lower()
        if not _SLUG_RE.match(slug):
            raise ValueError(f"invalid slug {slug!r}")

    # Check for canary
    raw_tier = str(tier).strip().lower() if not isinstance(tier, EnvironmentTier) else tier.value
    if is_canary or raw_tier in ("canary", "deploy-v2-canary"):
        return f"{CANARY_SLOT}.{base_domain}"

    # Handle legacy canary PR 999
    if slot_id is not None:
        slot_str = str(slot_id).strip().lower()
        if slot_str in ("999", "pr-999", "canary-preview"):
            warnings.warn(
                f"Legacy canary slot {slot_id!r} mapped to {CANARY_SLOT}. "
                "Use CANARY_SLOT or tier='canary'.",
                DeprecationWarning,
                stacklevel=2,
            )
            return f"{CANARY_SLOT}.{base_domain}"

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
            return f"{slug}-preview-{clean_slot}.{base_domain}"
        return f"{slug}-preview.{base_domain}"

    # Local / test tiers
    return f"{slug}-{canonical_tier.value.replace('_', '-')}.{base_domain}"


def resolve_dokploy_domains(
    preference: AppRoutePreference,
    tier: EnvironmentTier | str,
    *,
    base_domain: str = DEFAULT_BASE_DOMAIN,
    slot_id: str | int | None = None,
    is_canary: bool = False,
    https: bool = True,
) -> list[DokployDomainSpec]:
    """Compute concrete Dokploy domain specifications from app preference and environment."""
    host = resolve_app_hostname(
        preference,
        tier,
        base_domain=base_domain,
        slot_id=slot_id,
        is_canary=is_canary,
    )
    specs: list[DokployDomainSpec] = []
    for endpoint in preference.endpoints:
        specs.append(
            DokployDomainSpec(
                host=host,
                port=endpoint.port,
                path=endpoint.path,
                service_name=endpoint.service_name,
                https=https,
            )
        )
    return specs


def resolve_service_url(
    slug_or_preference: str | AppRoutePreference,
    tier: EnvironmentTier | str,
    *,
    base_domain: str = DEFAULT_BASE_DOMAIN,
    path: str = "/",
    slot_id: str | int | None = None,
    is_canary: bool = False,
    scheme: str = "https",
) -> str:
    """Compute the concrete public URL for a service endpoint."""
    host = resolve_app_hostname(
        slug_or_preference,
        tier,
        base_domain=base_domain,
        slot_id=slot_id,
        is_canary=is_canary,
    )
    clean_path = path if path.startswith("/") else f"/{path}"
    return f"{scheme}://{host}{clean_path}"
