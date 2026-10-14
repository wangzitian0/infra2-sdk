import pytest

from infra2_sdk import __version__
from infra2_sdk.runtime.health import DependencyStatus
from infra2_sdk.runtime.http import HttpClientSettings, create_http_client, probe_http


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


def test_standard_http_client_sets_the_user_agent_and_extra_headers() -> None:
    sync_client = create_http_client(headers={"X-Test": "1"})
    assert sync_client.headers["user-agent"] == f"infra2-sdk/{__version__}"
    assert sync_client.headers["x-test"] == "1"
    sync_client.close()


@pytest.mark.asyncio
async def test_create_async_http_client() -> None:
    async_client = create_http_client(async_client=True, headers={"X-Async": "1"})
    assert async_client.headers["x-async"] == "1"
    await async_client.aclose()


def test_http_settings_keep_their_defaults() -> None:
    settings = HttpClientSettings()
    assert settings.timeout_seconds == 10.0
    assert settings.connect_timeout_seconds == 5.0
    assert settings.max_connections == 100
    assert settings.max_keepalive_connections == 20
    assert settings.connect_retries == 0
    assert settings.user_agent == f"infra2-sdk/{__version__}"
    assert settings.follow_redirects is False


def test_http_settings_build_from_explicit_values() -> None:
    settings = HttpClientSettings(
        timeout_seconds=20,
        connect_timeout_seconds=3,
        max_connections=50,
        max_keepalive_connections=10,
        connect_retries=2,
        user_agent="my-app/1.0",
        follow_redirects=True,
    )
    assert settings.timeout_seconds == 20
    assert settings.connect_timeout_seconds == 3
    assert settings.max_connections == 50
    assert settings.max_keepalive_connections == 10
    assert settings.connect_retries == 2
    assert settings.user_agent == "my-app/1.0"
    assert settings.follow_redirects is True


def test_http_settings_have_no_environment_reader() -> None:
    assert not hasattr(HttpClientSettings, "from_env")


async def test_standard_async_http_client_closes_cleanly() -> None:
    async_client = create_http_client(async_client=True)
    assert async_client.headers["user-agent"] == f"infra2-sdk/{__version__}"
    await async_client.aclose()


async def test_http_probe_reports_status_and_errors() -> None:
    healthy = await probe_http(
        "https://example.test", name="analytics", client=Client(Response(429))
    )
    assert healthy.status is DependencyStatus.PRESENT
    assert healthy.name == "analytics"
    assert healthy.detail == "HTTP 429"
    failed = await probe_http("https://example.test", client=Client(Response(503)))
    assert failed.status is DependencyStatus.ABSENT
    error = await probe_http("https://example.test", client=Client(error=OSError("down")))
    assert "OSError: down" in error.detail


@pytest.mark.asyncio
async def test_http_probe_creates_and_closes_owned_client() -> None:
    settings = HttpClientSettings(timeout_seconds=0.5, connect_timeout_seconds=0.2)
    result = await probe_http("http://127.0.0.1:59999", settings=settings)
    assert result.status is DependencyStatus.ABSENT
    assert result.name == "http"


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
