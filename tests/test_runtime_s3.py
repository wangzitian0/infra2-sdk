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
    def __init__(self, *, error=None) -> None:
        self.error = error
        self.created = None

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
    )
    assert "secret" not in repr(settings)
    assert create_s3_client(settings, session=session) == "client"
    args, kwargs = session.args
    assert args == ("s3",)
    assert kwargs["endpoint_url"] == "http://minio:9000"
    assert kwargs["config"].s3["addressing_style"] == "path"


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
