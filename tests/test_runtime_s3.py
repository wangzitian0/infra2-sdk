import io

import pytest

from infra2_sdk.runtime.probes import DependencyStatus
from infra2_sdk.runtime.s3 import (
    S3Check,
    S3Settings,
    create_s3_client,
    ensure_bucket,
    is_not_found,
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


def test_s3_settings_load_standard_env_without_infra2_context() -> None:
    settings = S3Settings.from_env(
        {
            "OBJECT_STORAGE_PROTOCOL": "s3",
            "S3_BUCKET": "portable-artifacts",
            "AWS_ENDPOINT_URL_S3": "https://objects.example.test",
            "AWS_REGION": "ap-southeast-1",
            "AWS_ACCESS_KEY_ID": "key",
            "AWS_SECRET_ACCESS_KEY": "secret",
            "S3_ADDRESSING_STYLE": "virtual",
            "S3_CONNECT_TIMEOUT_SECONDS": "7.5",
            "S3_READ_TIMEOUT_SECONDS": "12",
        }
    )
    assert settings.endpoint_url == "https://objects.example.test"
    assert settings.region_name == "ap-southeast-1"
    assert settings.addressing_style == "virtual"
    assert settings.connect_timeout_seconds == 7.5
    assert settings.read_timeout_seconds == 12


def test_s3_legacy_aliases_and_default_credential_chain() -> None:
    settings = S3Settings.from_env(
        {
            "S3_BUCKET": "portable-artifacts",
            "S3_ENDPOINT": "http://minio:9000",
            "S3_REGION": "us-east-2",
        }
    )
    assert settings.endpoint_url == "http://minio:9000"
    assert settings.region_name == "us-east-2"
    assert settings.access_key_id is None


def test_plain_aws_environment_preserves_the_boto3_provider_chain() -> None:
    settings = S3Settings.from_env({"S3_BUCKET": "portable-artifacts"})
    assert settings.endpoint_url is None
    assert settings.region_name is None
    assert settings.addressing_style is None
    assert settings.access_key_id is None

    session = Session()
    create_s3_client(settings, session=session)
    _, kwargs = session.args
    assert kwargs["endpoint_url"] is None
    assert kwargs["region_name"] is None
    assert kwargs["aws_access_key_id"] is None
    assert kwargs["config"].s3 is None


@pytest.mark.parametrize(
    "environ,message",
    [
        ({"S3_BUCKET": "portable-artifacts", "OBJECT_STORAGE_PROTOCOL": "gcs"}, "unsupported"),
        (
            {"S3_BUCKET": "portable-artifacts", "S3_CONNECT_TIMEOUT_SECONDS": "fast"},
            "floating-point",
        ),
        ({"S3_BUCKET": "portable-artifacts", "S3_CONNECT_TIMEOUT_SECONDS": "nan"}, "finite"),
        ({"S3_BUCKET": "portable-artifacts", "AWS_ACCESS_KEY_ID": "key"}, "provided together"),
        ({"S3_BUCKET": "portable-artifacts", "AWS_SESSION_TOKEN": "token"}, "session token"),
        ({"S3_BUCKET": "portable-artifacts", "AWS_ENDPOINT_URL_S3": "minio:9000"}, "http"),
        ({"S3_BUCKET": "portable-artifacts", "AWS_ENDPOINT_URL_S3": "https://"}, "host"),
        ({"S3_BUCKET": "portable-artifacts", "AWS_ENDPOINT_URL_S3": "https://:9000"}, "host"),
        (
            {"S3_BUCKET": "portable-artifacts", "AWS_ENDPOINT_URL_S3": "https://bad host"},
            "host",
        ),
        (
            {
                "S3_BUCKET": "portable-artifacts",
                "AWS_ENDPOINT_URL_S3": "https://user:password@objects.example.test",
            },
            "credentials",
        ),
    ],
)
def test_s3_from_env_rejects_invalid_configuration(environ, message) -> None:
    with pytest.raises(ValueError, match=message):
        S3Settings.from_env(environ)


def test_probe_and_bucket_primitives() -> None:
    settings = S3Settings(bucket="runtime-canary", region_name="ap-southeast-1")
    healthy = Client()
    assert S3Check(settings, client=healthy).probe().status is DependencyStatus.PRESENT
    missing = Client(error=NotFound())
    assert S3Check(settings, client=missing).probe().status is DependencyStatus.ABSENT
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
        ({"addressing_style": "invalid"}, "addressing_style"),
        ({"access_key_id": "key"}, "provided together"),
    ],
)
def test_s3_settings_validation(changes, message) -> None:
    values = {"bucket": "runtime-canary"} | changes
    with pytest.raises(ValueError, match=message):
        S3Settings(**values)
