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
