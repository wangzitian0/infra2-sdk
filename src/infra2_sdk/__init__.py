"""Stable contracts shared by infra2 and application repositories."""

from importlib.metadata import PackageNotFoundError, version

from infra2_sdk.deploy import build_deploy_request
from infra2_sdk.routing import (
    CANARY_SLOT,
    DEFAULT_BASE_DOMAIN,
    LEGACY_CANARY_PR,
    AppRoutePreference,
    DokployDomainSpec,
    RouteEndpoint,
    resolve_app_hostname,
    resolve_dokploy_domains,
    resolve_service_url,
)
from infra2_sdk.transport import (
    HttpResponse,
    HttpTransport,
    urllib_transport,
)

try:
    __version__ = version("infra2-sdk")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.0.0"

__all__ = [
    "AppRoutePreference",
    "CANARY_SLOT",
    "DEFAULT_BASE_DOMAIN",
    "DokployDomainSpec",
    "HttpResponse",
    "HttpTransport",
    "LEGACY_CANARY_PR",
    "RouteEndpoint",
    "__version__",
    "build_deploy_request",
    "images",
    "resolve_app_hostname",
    "resolve_dokploy_domains",
    "resolve_service_url",
    "routing",
    "urllib_transport",
]
