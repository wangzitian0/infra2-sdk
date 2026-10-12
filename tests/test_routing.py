import warnings

import pytest

from infra2_sdk.routing import (
    CANARY_SLOT,
    DEFAULT_BASE_DOMAIN,
    AppRoutePreference,
    DokployDomainSpec,
    RouteEndpoint,
    resolve_app_hostname,
    resolve_dokploy_domains,
    resolve_service_url,
)
from infra2_sdk.runtime.environment import EnvironmentTier


def test_route_endpoint_validation():
    endpoint = RouteEndpoint(port=3000, path="/", service_name="web")
    assert endpoint.port == 3000
    assert endpoint.path == "/"
    assert endpoint.service_name == "web"

    with pytest.raises(ValueError, match="port must be between"):
        RouteEndpoint(port=70000)

    with pytest.raises(ValueError, match="path must start with"):
        RouteEndpoint(port=80, path="api")


def test_app_route_preference_validation():
    pref = AppRoutePreference(
        slug="report",
        endpoints=(
            RouteEndpoint(port=3000, path="/", service_name="frontend"),
            RouteEndpoint(port=8000, path="/api", service_name="backend"),
        ),
    )
    assert pref.slug == "report"
    assert len(pref.endpoints) == 2

    # custom prod domain
    pref_custom = AppRoutePreference(
        slug="truealpha",
        endpoints=(RouteEndpoint(port=3000),),
        custom_prod_domain="truealpha.club",
    )
    assert pref_custom.custom_prod_domain == "truealpha.club"

    # invalid slug
    with pytest.raises(ValueError, match="invalid slug"):
        AppRoutePreference(slug="Report_Invalid")

    # invalid custom domain
    with pytest.raises(ValueError, match="invalid custom_prod_domain"):
        AppRoutePreference(slug="ok", custom_prod_domain="bad domain")


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
    assert resolve_app_hostname("report", "canary") == f"{CANARY_SLOT}.{DEFAULT_BASE_DOMAIN}"
    assert (
        resolve_app_hostname("report", EnvironmentTier.PREVIEW, is_canary=True)
        == f"{CANARY_SLOT}.{DEFAULT_BASE_DOMAIN}"
    )

    # Slot 999 is an ordinary preview slot: the legacy canary mapping is gone.
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert (
            resolve_app_hostname("report", EnvironmentTier.PREVIEW, slot_id=999)
            == f"report-preview-999.{DEFAULT_BASE_DOMAIN}"
        )
        assert (
            resolve_app_hostname("report", EnvironmentTier.PREVIEW, slot_id="pr-999")
            == f"report-preview-pr-999.{DEFAULT_BASE_DOMAIN}"
        )

    # Canonical CANARY_SLOT slot_id resolves to the canary host and raises no warning
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        host = resolve_app_hostname("report", EnvironmentTier.PREVIEW, slot_id=CANARY_SLOT)
        assert host == f"{CANARY_SLOT}.{DEFAULT_BASE_DOMAIN}"


def test_resolve_dokploy_domains():
    pref = AppRoutePreference(
        slug="report",
        endpoints=(
            RouteEndpoint(port=3000, path="/", service_name="frontend"),
            RouteEndpoint(port=8000, path="/api", service_name="backend"),
        ),
    )
    specs = resolve_dokploy_domains(pref, EnvironmentTier.STAGING)
    assert len(specs) == 2
    assert specs[0] == DokployDomainSpec(
        host="report-staging.zitian.party",
        port=3000,
        path="/",
        service_name="frontend",
        https=True,
    )
    assert specs[1] == DokployDomainSpec(
        host="report-staging.zitian.party",
        port=8000,
        path="/api",
        service_name="backend",
        https=True,
    )

    d0 = specs[0].to_dict()
    assert d0 == {
        "host": "report-staging.zitian.party",
        "port": 3000,
        "path": "/",
        "serviceName": "frontend",
        "https": True,
    }


def test_resolve_service_url():
    url_prod = resolve_service_url("report", EnvironmentTier.PRODUCTION, path="/api/health")
    assert url_prod == "https://report.zitian.party/api/health"

    url_staging = resolve_service_url("report", EnvironmentTier.STAGING)
    assert url_staging == "https://report-staging.zitian.party/"

    url_canary = resolve_service_url("report", "canary", path="/healthz")
    assert url_canary == "https://canary-preview.zitian.party/healthz"


def test_legacy_canary_pr_constant_is_removed() -> None:
    import infra2_sdk
    import infra2_sdk.routing

    assert not hasattr(infra2_sdk.routing, "LEGACY_CANARY_PR")
    assert not hasattr(infra2_sdk, "LEGACY_CANARY_PR")
    assert "LEGACY_CANARY_PR" not in infra2_sdk.__all__
