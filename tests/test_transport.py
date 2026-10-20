from __future__ import annotations

import io
import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

from infra2_sdk.transport import HttpResponse, urllib_transport


def test_urllib_transport_success() -> None:
    transport = urllib_transport(timeout=10.0)

    mock_response = MagicMock()
    mock_response.status = 200
    mock_response.headers = {"Content-Type": "application/json", "X-Custom": "val"}
    mock_response.read.return_value = b'{"ok": true}'
    mock_response.__enter__.return_value = mock_response
    mock_response.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_response) as mock_urlopen:
        res = transport("GET", "https://api.example.com/test", {"Accept": "application/json"}, None)
        assert isinstance(res, HttpResponse)
        assert res.status == 200
        assert res.headers["content-type"] == "application/json"
        assert res.headers["x-custom"] == "val"
        assert res.body == b'{"ok": true}'
        assert mock_urlopen.call_count == 1
        req = mock_urlopen.call_args[0][0]
        assert req.get_method() == "GET"
        assert req.full_url == "https://api.example.com/test"


def test_urllib_transport_http_error_returns_status_and_closes() -> None:
    transport = urllib_transport(timeout=10.0)

    fp = io.BytesIO(b"Not Found")
    error = urllib.error.HTTPError(
        url="https://api.example.com/missing",
        code=404,
        msg="Not Found",
        hdrs={"Content-Type": "text/plain"},  # type: ignore[arg-type]
        fp=fp,
    )
    # Spy on fp.close
    fp_close = error.close

    closed = False

    def close_wrapper():
        nonlocal closed
        closed = True
        fp_close()

    error.close = close_wrapper

    with patch("urllib.request.urlopen", side_effect=error):
        res = transport("POST", "https://api.example.com/missing", {}, b"data")
        assert isinstance(res, HttpResponse)
        assert res.status == 404
        assert res.body == b"Not Found"
        assert closed, "error.close() must be called on HTTPError"


def test_classify_transport_error_covers_closed_enum_kinds() -> None:
    import socket
    import ssl
    from decimal import Decimal

    from infra2_sdk.transport import (
        ThrottlePolicy,
        TransportErrorKind,
        TransportFailure,
        classify_transport_error,
        decode_decimal_json,
        paced_transport,
    )

    dns_err = urllib.error.URLError(socket.gaierror(8, "nodename nor servname provided"))
    f_dns = classify_transport_error(dns_err)
    assert isinstance(f_dns, TransportFailure)
    assert f_dns.kind == TransportErrorKind.DNS
    assert "dns:" in str(f_dns)
    assert classify_transport_error(f_dns) is f_dns

    timeout_err = urllib.error.URLError(TimeoutError("timed out"))
    assert classify_transport_error(timeout_err).kind == TransportErrorKind.TIMEOUT

    refused_err = urllib.error.URLError(ConnectionRefusedError("Connection refused"))
    assert classify_transport_error(refused_err).kind == TransportErrorKind.REFUSED

    tls_err = urllib.error.URLError(ssl.SSLError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF"))
    assert classify_transport_error(tls_err).kind == TransportErrorKind.TLS

    conn_err = urllib.error.URLError(ConnectionResetError("Connection reset by peer"))
    assert classify_transport_error(conn_err).kind == TransportErrorKind.CONNECTION

    decoded = decode_decimal_json(b'{"close": 305.92999, "volume": 100}')
    assert decoded == {"close": Decimal("305.92999"), "volume": 100}
    assert isinstance(decoded["close"], Decimal)
    assert decode_decimal_json('{"price": 12.34}')["price"] == Decimal("12.34")

    import pytest

    with pytest.raises(ValueError, match="requests_per_minute"):
        ThrottlePolicy(requests_per_minute=0)
    with pytest.raises(ValueError, match="min_spacing_seconds"):
        ThrottlePolicy(requests_per_minute=8, min_spacing_seconds=-1.0)
    with pytest.raises(ValueError, match="window_seconds"):
        ThrottlePolicy(requests_per_minute=8, window_seconds=0.0)

    now = 100.0
    sleeps: list[float] = []

    def fake_clock() -> float:
        return now

    def fake_sleep(seconds: float) -> None:
        nonlocal now
        sleeps.append(round(seconds, 3))
        now += seconds

    inner_calls: list[str] = []

    def fake_send(method: str, url: str, headers: object, body: bytes | None) -> HttpResponse:
        inner_calls.append(url)
        return HttpResponse(200, {}, b"{}")

    policy = ThrottlePolicy(requests_per_minute=2, min_spacing_seconds=5.0, window_seconds=60.0)
    throttled = paced_transport(fake_send, policy, clock=fake_clock, sleep=fake_sleep)

    assert throttled("GET", "https://example.com/1", {}, None).status == 200
    assert sleeps == []
    assert throttled("GET", "https://example.com/2", {}, None).status == 200
    assert sleeps == [5.0]
    assert throttled("GET", "https://example.com/3", {}, None).status == 200
    assert sleeps == [5.0, 55.0]
    assert len(inner_calls) == 3
