"""Minimal injectable HTTP transport shared by the open-protocol adapters."""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass


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
            return HttpResponse(
                error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()
            )

    return send
