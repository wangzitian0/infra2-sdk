import warnings

import pytest

from infra2_sdk.routing import (
    _DEFAULT_BASE_DOMAIN,
    CANARY_SLOT,
    PreviewStackCoordinates,
    derive_preview_coordinates,
    resolve_app_hostname,
    resolve_service_url,
)
from infra2_sdk.runtime.environment import EnvironmentTier


def test_resolve_app_hostname_rejects_invalid_slug() -> None:
    with pytest.raises(ValueError, match="invalid slug"):
        resolve_app_hostname("bad slug!", EnvironmentTier.STAGING)
    with pytest.raises(ValueError, match="invalid slug"):
        resolve_app_hostname("", EnvironmentTier.STAGING)


def test_resolve_app_hostname_standards():
    # Production without custom domain
    assert resolve_app_hostname("report", EnvironmentTier.PRODUCTION) == "report.zitian.party"
    assert resolve_app_hostname("report", "prod") == "report.zitian.party"

    # Production with custom domain
    assert (
        resolve_app_hostname(
            "truealpha",
            EnvironmentTier.PRODUCTION,
            custom_prod_domain="truealpha.club",
        )
        == "truealpha.club"
    )

    # Staging
    assert resolve_app_hostname("report", EnvironmentTier.STAGING) == "report-staging.zitian.party"
    assert resolve_app_hostname("report", "staging") == "report-staging.zitian.party"
    assert (
        resolve_app_hostname(
            "truealpha",
            EnvironmentTier.STAGING,
            custom_prod_domain="truealpha.club",
        )
        == "truealpha-staging.zitian.party"
    )

    # Preview default
    assert resolve_app_hostname("report", EnvironmentTier.PREVIEW) == "report-preview.zitian.party"
    assert resolve_app_hostname("report", "preview") == "report-preview.zitian.party"

    # Preview with slot
    assert (
        resolve_app_hostname("report", EnvironmentTier.PREVIEW, slot_id="slot1")
        == "report-preview-slot1.zitian.party"
    )


def test_canary_hostname_resolution_and_slot_999_is_not_special():
    # Canonical canary
    assert (
        resolve_app_hostname("report", "canary") == f"report-{CANARY_SLOT}.{_DEFAULT_BASE_DOMAIN}"
    )
    assert (
        resolve_app_hostname("report", EnvironmentTier.PREVIEW, is_canary=True)
        == f"report-{CANARY_SLOT}.{_DEFAULT_BASE_DOMAIN}"
    )

    # Slot 999 is an ordinary PR alias: the legacy canary mapping is gone.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert (
            resolve_app_hostname("report", EnvironmentTier.PREVIEW, slot_id=999)
            == f"report-pr-999.{_DEFAULT_BASE_DOMAIN}"
        )
        assert (
            resolve_app_hostname("report", EnvironmentTier.PREVIEW, slot_id="pr-999")
            == f"report-pr-999.{_DEFAULT_BASE_DOMAIN}"
        )

    # Canonical CANARY_SLOT slot_id resolves to the canary host and raises no warning
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        host = resolve_app_hostname("report", EnvironmentTier.PREVIEW, slot_id=CANARY_SLOT)
        assert host == f"report-{CANARY_SLOT}.{_DEFAULT_BASE_DOMAIN}"


def test_resolve_service_url():
    url_prod = resolve_service_url("report", EnvironmentTier.PRODUCTION, path="/api/health")
    assert url_prod == "https://report.zitian.party/api/health"

    url_staging = resolve_service_url("report", EnvironmentTier.STAGING)
    assert url_staging == "https://report-staging.zitian.party/"

    url_canary = resolve_service_url("report", "canary", path="/healthz")
    assert url_canary == "https://report-canary-preview.zitian.party/healthz"


def test_legacy_canary_pr_constant_is_removed() -> None:
    import infra2_sdk
    import infra2_sdk.routing

    assert not hasattr(infra2_sdk.routing, "LEGACY_CANARY_PR")
    assert not hasattr(infra2_sdk, "LEGACY_CANARY_PR")
    assert "LEGACY_CANARY_PR" not in infra2_sdk.__all__


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"tier": "branch-main"}, "report-branch-main.zitian.party"),
        ({"tier": "pr_5"}, "report-pr-5.zitian.party"),
        ({"tier": "commit_1ab32d5"}, "report-commit-1ab32d5.zitian.party"),
        ({"tier": "preview", "slot_id": "branch_main"}, "report-branch-main.zitian.party"),
        ({"tier": "preview", "slot_id": 999}, "report-pr-999.zitian.party"),
        ({"tier": "preview", "slot_id": "slot1"}, "report-preview-slot1.zitian.party"),
        ({"tier": "canary_preview"}, "report-canary-preview.zitian.party"),
    ],
)
def test_preview_aliases_share_one_hostname_grammar(kwargs: dict, expected: str) -> None:
    assert resolve_app_hostname("report", **kwargs) == expected


def test_derive_preview_coordinates_production() -> None:
    coords = derive_preview_coordinates("finance-report", EnvironmentTier.PRODUCTION)
    assert isinstance(coords, PreviewStackCoordinates)
    assert coords.slug == "finance-report"
    assert coords.tier is EnvironmentTier.PRODUCTION
    assert coords.deployment_environment == "production"
    assert coords.env_suffix == ""
    assert coords.env_domain_suffix == ""
    assert coords.app_host == "finance-report.zitian.party"
    assert coords.preview_url == "https://finance-report.zitian.party"
    assert coords.cpu_shares == 1024
    assert coords.network_suffix == ""
    assert (
        coords.ephemeral_database_url
        == "postgresql://ephemeral:ephemeral@postgres:5432/finance_report_preview"
    )


def test_derive_preview_coordinates_staging() -> None:
    coords = derive_preview_coordinates("finance-report", EnvironmentTier.STAGING)
    assert coords.tier is EnvironmentTier.STAGING
    assert coords.deployment_environment == "staging"
    assert coords.env_suffix == "-staging"
    assert coords.env_domain_suffix == "-staging"
    assert coords.app_host == "finance-report-staging.zitian.party"
    assert coords.preview_url == "https://finance-report-staging.zitian.party"
    assert coords.cpu_shares == 512
    assert coords.network_suffix == "-staging"


def test_derive_preview_coordinates_canary() -> None:
    coords1 = derive_preview_coordinates("report", EnvironmentTier.PREVIEW, is_canary=True)
    coords2 = derive_preview_coordinates("report", "canary")
    coords3 = derive_preview_coordinates("report", EnvironmentTier.PREVIEW, slot_id=CANARY_SLOT)
    for coords in (coords1, coords2, coords3):
        assert coords.tier is EnvironmentTier.PREVIEW
        assert coords.deployment_environment == CANARY_SLOT
        assert coords.env_suffix == f"-{CANARY_SLOT}"
        assert coords.env_domain_suffix == f"-{CANARY_SLOT}"
        assert coords.cpu_shares == 256
        assert coords.network_suffix == f"-{CANARY_SLOT}"
        assert coords.app_host == f"report-{CANARY_SLOT}.zitian.party"


def test_derive_preview_coordinates_preview_variations() -> None:
    # Integer PR slot
    pr_coords = derive_preview_coordinates("demo", EnvironmentTier.PREVIEW, slot_id=113)
    assert pr_coords.tier is EnvironmentTier.PREVIEW
    assert pr_coords.deployment_environment == "pr-113"
    assert pr_coords.env_suffix == "-pr-113"
    assert pr_coords.app_host == "demo-pr-113.zitian.party"
    assert pr_coords.cpu_shares == 256
    assert pr_coords.network_suffix == "-pr-113"

    # Preview alias in tier
    branch_coords = derive_preview_coordinates("demo", "branch-feat-x")
    assert branch_coords.deployment_environment == "branch-feat-x"
    assert branch_coords.env_suffix == "-branch-feat-x"

    # Default preview without slot
    default_coords = derive_preview_coordinates("demo", "preview")
    assert default_coords.deployment_environment == "preview"
    assert default_coords.env_suffix == "-preview"

    # Custom string slot
    custom_coords = derive_preview_coordinates("demo", "preview", slot_id="custom-slot")
    assert custom_coords.deployment_environment == "preview-custom-slot"
    assert custom_coords.env_suffix == "-preview-custom-slot"
