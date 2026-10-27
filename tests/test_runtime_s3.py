import io

import pytest

from infra2_sdk.runtime.health import DependencyStatus
from infra2_sdk.runtime.s3 import (
    S3Settings,
    create_s3_client,
    ensure_bucket,
    is_not_found,
    probe_s3,
    read_object_bytes,
    redact_presigned_url,
)


class Client:
    def __init__(self, *, error=None, region_name=None) -> None:
        self.error = error
        self.created = None
        self.meta = type("Meta", (), {"region_name": region_name})()

    def head_bucket(self, **kwargs):
        if self.error:
            raise self.error
        return kwargs

    def create_bucket(self, **kwargs):
        self.created = kwargs

    def get_object(self, **_kwargs):
        return {"Body": io.BytesIO(b"content")}


class NotFound(Exception):
    response = {"Error": {"Code": "NoSuchBucket"}}


class Session:
    def __init__(self) -> None:
        self.args = None

    def client(self, *args, **kwargs):
        self.args = (args, kwargs)
        return "client"


def test_standard_client_factory_returns_session_client() -> None:
    session = Session()
    settings = S3Settings(
        bucket="runtime-canary",
        endpoint_url="http://minio:9000",
        access_key_id="key",
        secret_access_key="secret",
        addressing_style="path",
    )
    assert "secret" not in repr(settings)
    assert create_s3_client(settings, session=session) == "client"
    args, kwargs = session.args
    assert args == ("s3",)
    assert kwargs["endpoint_url"] == "http://minio:9000"
    assert kwargs["config"].s3["addressing_style"] == "path"


def test_s3_settings_build_from_explicit_values() -> None:
    settings = S3Settings(
        bucket="portable-artifacts",
        endpoint_url="https://objects.example.test",
        access_key_id="key",
        secret_access_key="secret",
        session_token="token",
        region_name="ap-southeast-1",
        connect_timeout_seconds=7.5,
        read_timeout_seconds=12,
        addressing_style="virtual",
    )
    assert settings.bucket == "portable-artifacts"
    assert settings.endpoint_url == "https://objects.example.test"
    assert settings.access_key_id == "key"
    assert settings.session_token == "token"
    assert settings.region_name == "ap-southeast-1"
    assert settings.addressing_style == "virtual"
    assert settings.connect_timeout_seconds == 7.5
    assert settings.read_timeout_seconds == 12
    assert "secret" not in repr(settings) and "token" not in repr(settings)


def test_s3_settings_keep_their_defaults_and_have_no_environment_reader() -> None:
    settings = S3Settings(bucket="portable-artifacts")
    assert settings.endpoint_url is None
    assert settings.access_key_id is None
    assert settings.secret_access_key is None
    assert settings.session_token is None
    assert settings.region_name is None
    assert settings.addressing_style is None
    assert settings.connect_timeout_seconds == 5.0
    assert settings.read_timeout_seconds == 5.0
    assert not hasattr(S3Settings, "from_env")


def test_default_settings_preserve_the_boto3_provider_chain() -> None:
    session = Session()
    create_s3_client(S3Settings(bucket="portable-artifacts"), session=session)
    _, kwargs = session.args
    assert kwargs["endpoint_url"] is None
    assert kwargs["region_name"] is None
    assert kwargs["aws_access_key_id"] is None
    assert kwargs["config"].s3 is None


def test_probe_and_bucket_primitives() -> None:
    settings = S3Settings(bucket="runtime-canary", region_name="ap-southeast-1")
    healthy = Client()
    assert probe_s3(settings, client=healthy).status is DependencyStatus.PRESENT
    missing = Client(error=NotFound())
    assert probe_s3(settings, client=missing).status is DependencyStatus.ABSENT
    ensure_bucket(settings, client=missing, allow_create=True)
    assert missing.created == {
        "Bucket": "runtime-canary",
        "CreateBucketConfiguration": {"LocationConstraint": "ap-southeast-1"},
    }
    with pytest.raises(NotFound):
        ensure_bucket(settings, client=Client(error=NotFound()), allow_create=False)


def test_bucket_creation_uses_region_resolved_by_boto_client() -> None:
    settings = S3Settings(bucket="runtime-canary")
    missing = Client(error=NotFound(), region_name="eu-west-1")
    ensure_bucket(settings, client=missing, allow_create=True)
    assert missing.created == {
        "Bucket": "runtime-canary",
        "CreateBucketConfiguration": {"LocationConstraint": "eu-west-1"},
    }


def test_read_redact_and_error_classification() -> None:
    assert read_object_bytes(Client(), bucket="bucket", key="key") == b"content"
    assert is_not_found(NotFound()) is True
    assert is_not_found(ValueError("bad")) is False
    url = "https://s3.example.test/bucket/key?X-Amz-Signature=secret#fragment"
    assert redact_presigned_url(url) == ("https://s3.example.test/bucket/key?signature=<redacted>")
    assert redact_presigned_url(None) is None


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"bucket": "Bad_Bucket"}, "bucket"),
        ({"connect_timeout_seconds": 0}, "timeouts"),
        ({"read_timeout_seconds": 0}, "timeouts"),
        ({"addressing_style": "invalid"}, "addressing_style"),
        ({"access_key_id": "key"}, "provided together"),
        ({"session_token": "token"}, "session token"),
        ({"endpoint_url": "minio:9000"}, "http"),
        ({"endpoint_url": "https://"}, "host"),
        ({"endpoint_url": "https://:9000"}, "host"),
        ({"endpoint_url": "https://bad host"}, "host"),
        ({"endpoint_url": "https://user:password@objects.example.test"}, "credentials"),
    ],
)
def test_s3_settings_validation(changes, message) -> None:
    values = {"bucket": "runtime-canary"} | changes
    with pytest.raises(ValueError, match=message):
        S3Settings(**values)


@pytest.mark.parametrize(
    "error,expected_status",
    [
        (None, DependencyStatus.PRESENT),
        (RuntimeError("connection reset"), DependencyStatus.ABSENT),
    ],
)
def test_probe_s3_closes_owned_client(
    error, expected_status, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed = []

    class FakeOwnedClient:
        def head_bucket(self, **kwargs):
            if error is not None:
                raise error
            return kwargs

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "infra2_sdk.runtime.s3.create_s3_client",
        lambda settings: FakeOwnedClient(),
    )
    settings = S3Settings(bucket="runtime-canary")
    result = probe_s3(settings)
    assert result.status is expected_status
    assert closed == [True], "owned S3 client must be closed after probe"


def test_probe_s3_handles_client_creation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "infra2_sdk.runtime.s3.create_s3_client",
        lambda settings: (_ for _ in ()).throw(RuntimeError("missing boto3")),
    )
    settings = S3Settings(bucket="runtime-canary")
    result = probe_s3(settings)
    assert result.status is DependencyStatus.ABSENT
    assert "missing boto3" in result.detail


def test_probe_s3_swallows_client_close_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeBrokenCloseClient:
        def head_bucket(self, **kwargs):
            return kwargs

        def close(self):
            raise OSError("socket fd reset")

    monkeypatch.setattr(
        "infra2_sdk.runtime.s3.create_s3_client",
        lambda settings: FakeBrokenCloseClient(),
    )
    settings = S3Settings(bucket="runtime-canary")
    result = probe_s3(settings)
    assert result.status is DependencyStatus.PRESENT
    assert result.detail == "bucket accessible"


def test_ensure_bucket_closes_owned_client_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = []

    class FakeOwnedClient:
        def head_bucket(self, **kwargs):
            return kwargs

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "infra2_sdk.runtime.s3.create_s3_client",
        lambda settings: FakeOwnedClient(),
    )
    settings = S3Settings(bucket="runtime-canary")
    ensure_bucket(settings)
    assert closed == [True], "owned S3 client must be closed after successful ensure_bucket"


def test_ensure_bucket_closes_owned_client_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = []

    class FakeOwnedClient:
        def head_bucket(self, **kwargs):
            raise RuntimeError("head bucket network error")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(
        "infra2_sdk.runtime.s3.create_s3_client",
        lambda settings: FakeOwnedClient(),
    )
    settings = S3Settings(bucket="runtime-canary")
    with pytest.raises(RuntimeError, match="head bucket network error"):
        ensure_bucket(settings)
    assert closed == [True], "owned S3 client must be closed even when ensure_bucket raises"


@pytest.mark.parametrize("operation", ["ensure_bucket", "probe_s3"])
def test_unowned_client_is_not_closed(operation: str) -> None:
    closed = []
    calls = []

    class ExternalClient:
        def head_bucket(self, **kwargs):
            calls.append(kwargs)
            return kwargs

        def close(self):
            closed.append(True)

    settings = S3Settings(bucket="runtime-canary")
    client = ExternalClient()
    if operation == "ensure_bucket":
        ensure_bucket(settings, client=client)
        assert calls == [{"Bucket": "runtime-canary"}]
    else:
        result = probe_s3(settings, client=client)
        assert result.status is DependencyStatus.PRESENT
        assert calls == [{"Bucket": "runtime-canary"}]
    assert closed == [], f"unowned S3 client must not be closed by {operation}"


@pytest.mark.parametrize(
    "removed_field",
    [
        "public_endpoint_url",
        "public_bucket",
        "public_access_key_id",
        "public_secret_access_key",
        "presign_expiry_seconds",
        "raw_prefix",
        "documents_prefix",
    ],
)
def test_s3_settings_omits_unused_application_owned_fields(removed_field: str) -> None:
    settings = S3Settings(bucket="valid-bucket")
    assert not hasattr(settings, removed_field)
    with pytest.raises(TypeError, match=removed_field):
        S3Settings(bucket="valid-bucket", **{removed_field: "value"})  # type: ignore[arg-type]
