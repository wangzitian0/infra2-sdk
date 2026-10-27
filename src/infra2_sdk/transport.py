"""Minimal injectable HTTP transport shared by the open-protocol adapters."""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_ACCEPT = "application/vnd.github+json"


def _github_headers(*, token: str = "", user_agent: str, content_type: str = "") -> dict[str, str]:
    """Build standard GitHub REST API headers with optional Bearer token and Content-Type."""
    headers = {
        "Accept": GITHUB_ACCEPT,
        "User-Agent": user_agent,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if content_type:
        headers["Content-Type"] = content_type
    return headers


def _github_api_url(path: str) -> str:
    """Resolve a GitHub API path or full HTTPS URL against ``GITHUB_API_BASE``."""
    return path if path.startswith("https://") else f"{GITHUB_API_BASE}{path}"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


HttpTransport = Callable[[str, str, Mapping[str, str], bytes | None], HttpResponse]


def urllib_transport(*, timeout: float = 30.0) -> HttpTransport:
    """Return a transport over the standard library; error statuses are returned, not raised."""

    def send(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> HttpResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
                headers = {k.lower(): v for k, v in response.headers.items()}
                return HttpResponse(response.status, headers, response.read())
        except urllib.error.HTTPError as error:
            try:
                headers = {k.lower(): v for k, v in error.headers.items()}
                return HttpResponse(error.code, headers, error.read())
            finally:
                error.close()

    return send


__all__ = [
    "urllib_transport",
]
