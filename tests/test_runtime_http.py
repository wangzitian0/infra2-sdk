from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest

from infra2_sdk import __version__
from infra2_sdk.runtime.http import (
    HttpCheck,
    HttpClientSettings,
    create_http_client,
    parse_retry_after,
    probe_http,
    retryable_request,
)
from infra2_sdk.runtime.probes import DependencyStatus


class Response:
    def __init__(self, status_code) -> None:
        self.status_code = status_code


class Client:
    def __init__(self, response=None, error=None) -> None:
        self.response = response
        self.error = error

    async def get(self, _url):
        if self.error:
            raise self.error
        return self.response


def test_standard_http_clients_and_retry_policy() -> None:
    sync_client = create_http_client(headers={"X-Test": "1"})
    assert sync_client.headers["user-agent"] == f"infra2-sdk/{__version__}"
    assert sync_client.headers["x-test"] == "1"
    sync_client.close()

    assert retryable_request("GET", 503)
    assert retryable_request("POST", 429, has_idempotency_key=True)
    assert not retryable_request("POST", 503)
    assert not retryable_request("GET", 404)


def test_http_settings_load_from_generic_env() -> None:
    settings = HttpClientSettings.from_env(
        {
            "HTTP_TIMEOUT_SECONDS": "20",
            "HTTP_CONNECT_TIMEOUT_SECONDS": "3",
            "HTTP_MAX_CONNECTIONS": "50",
            "HTTP_MAX_KEEPALIVE_CONNECTIONS": "10",
            "HTTP_CONNECT_RETRIES": "2",
            "HTTP_FOLLOW_REDIRECTS": "true",
        }
    )
    assert settings.timeout_seconds == 20
    assert settings.connect_timeout_seconds == 3
    assert settings.max_connections == 50
    assert settings.max_keepalive_connections == 10
    assert settings.connect_retries == 2
    assert settings.follow_redirects is True


async def test_standard_async_http_client_closes_cleanly() -> None:
    async_client = create_http_client(async_client=True)
    assert async_client.headers["user-agent"] == f"infra2-sdk/{__version__}"
    await async_client.aclose()


async def test_http_probe_reports_status_and_errors() -> None:
    healthy = await HttpCheck(
        "analytics", "https://example.test", client=Client(Response(429))
    ).probe()
    assert healthy.status is DependencyStatus.PRESENT
    failed = await probe_http("https://example.test", client=Client(Response(503)))
    assert failed.status is DependencyStatus.ABSENT
    error = await probe_http("https://example.test", client=Client(error=OSError("down")))
    assert "OSError: down" in error.detail


def test_retry_after_supports_both_standard_forms() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    assert parse_retry_after("30", now=now) == 30
    assert parse_retry_after(format_datetime(now + timedelta(seconds=15)), now=now) == 15
    assert parse_retry_after("bad", now=now) is None
    assert parse_retry_after(None) is None
    assert parse_retry_after(format_datetime(now), now=datetime(2026, 1, 1)) == 0


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"timeout_seconds": 0}, "timeouts"),
        ({"max_connections": 0}, "limits"),
        ({"max_connections": 1, "max_keepalive_connections": 2}, "keepalive"),
        ({"connect_retries": -1}, "non-negative"),
    ],
)
def test_http_settings_validation(changes, message) -> None:
    with pytest.raises(ValueError, match=message):
        HttpClientSettings(**changes)


def test_http_check_requires_standard_absolute_url() -> None:
    with pytest.raises(ValueError, match="absolute"):
        HttpCheck("bad", "/relative")
