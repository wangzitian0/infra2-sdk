"""Minimal injectable HTTP transport shared by open-protocol adapters."""

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
    data = None if payload is None else json.dumps(dict(payload)).encode()
    req = urllib.request.Request(url, headers=dict(headers), data=data, method=method)  # noqa: S310
    with opener(req, timeout=timeout) as resp:  # type: ignore[attr-defined]
        raw = resp.read()
    text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
    return json.loads(text) if text.strip() else None


def _decode_github_contents(payload: object, *, require_encoding: bool = False) -> bytes:
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
    """Return a stdlib HTTP transport that returns error status codes instead of raising."""

    def send(method: str, url: str, headers: Mapping[str, str], body: bytes | None) -> HttpResponse:
        req = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
                return HttpResponse(
                    resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
                )
        except urllib.error.HTTPError as err:
            try:
                return HttpResponse(
                    err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()
                )
            finally:
                err.close()

    return send


__all__ = ["urllib_transport"]
