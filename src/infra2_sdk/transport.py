"""Minimal injectable HTTP transport shared by the open-protocol adapters."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

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


def _urlopen_json(
    url: str,
    *,
    headers: Mapping[str, str],
    method: str = "GET",
    payload: Mapping[str, object] | None = None,
    timeout: float = 20.0,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> Any:
    """Send an HTTP request via ``opener`` and decode a JSON response body (or None if empty)."""
    request = urllib.request.Request(  # noqa: S310
        url,
        headers=dict(headers),
        data=None if payload is None else json.dumps(dict(payload)).encode("utf-8"),
        method=method,
    )
    with opener(request, timeout=timeout) as response:  # type: ignore[attr-defined]
        raw = response.read()
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else str(raw)
    return json.loads(text) if text.strip() else None


def _extract_workflow_runs(payload: object) -> list[dict[str, Any]]:
    """Validate and return the ``workflow_runs`` list from a GitHub Actions runs payload."""
    if not isinstance(payload, Mapping):
        raise TypeError("GitHub workflow-runs response must be an object")
    runs = payload.get("workflow_runs")
    if not isinstance(runs, list) or not all(isinstance(run, Mapping) for run in runs):
        raise ValueError("GitHub workflow-runs response must contain a run list")
    return runs  # type: ignore[return-value]


def _decode_github_contents(payload: object, *, require_encoding: bool = False) -> bytes:
    """Extract and base64-decode ``content`` from a GitHub REST repository contents payload."""
    if not isinstance(payload, Mapping):
        raise TypeError("must contain a JSON object")
    content = payload.get("content")
    if (require_encoding and payload.get("encoding") != "base64") or not isinstance(content, str):
        raise KeyError("did not return file content")
    return base64.b64decode(content, validate=False)


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
