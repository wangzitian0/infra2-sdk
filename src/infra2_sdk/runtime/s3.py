"""Thin helpers over the open S3 API; object semantics remain application-owned."""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from infra2_sdk._wire import validate_http_url
from infra2_sdk.runtime._optional import require
from infra2_sdk.runtime.health import DependencyStatus, ProbeResult, _elapsed

_BUCKET_RE = re.compile(r"\A[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_NOT_FOUND_CODES = frozenset({"404", "NoSuchBucket", "NoSuchKey", "NotFound"})


@dataclass(frozen=True)
class S3Settings:
    bucket: str
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)
    region_name: str | None = None
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 5.0
    addressing_style: str | None = None

    def __post_init__(self) -> None:
        if not _BUCKET_RE.match(self.bucket) or ".." in self.bucket:
            raise ValueError("bucket must be a lowercase S3-compatible name")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("S3 timeouts must be positive")
        if self.addressing_style not in {None, "auto", "path", "virtual"}:
            raise ValueError("addressing_style must be auto, path, or virtual")
        if bool(self.access_key_id) != bool(self.secret_access_key):
            raise ValueError("explicit S3 access key and secret key must be provided together")
        if self.session_token and not self.access_key_id:
            raise ValueError("explicit S3 session token requires explicit access credentials")
        if self.endpoint_url:
            validate_http_url(self.endpoint_url, label="S3 endpoint URL")


def create_s3_client(settings: S3Settings, *, session: Any | None = None) -> Any:
    """Create and return the standard boto3 S3 client without performing I/O."""
    boto3 = require("boto3", extra="s3")
    botocore_config = require("botocore.config", extra="s3")
    s3_cfg = {"addressing_style": settings.addressing_style} if settings.addressing_style else None
    return (session or boto3).client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        aws_session_token=settings.session_token,
        region_name=settings.region_name,
        config=botocore_config.Config(
            signature_version="s3v4",
            connect_timeout=settings.connect_timeout_seconds,
            read_timeout=settings.read_timeout_seconds,
            s3=s3_cfg,
        ),
    )


def probe_s3(settings: S3Settings, *, client: Any | None = None) -> ProbeResult:
    started = time.perf_counter()
    owned = client is None
    s3 = client
    try:
        s3 = s3 or create_s3_client(settings)
        s3.head_bucket(Bucket=settings.bucket)
        return ProbeResult(
            "object_storage", DependencyStatus.PRESENT, "bucket accessible", _elapsed(started)
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            "object_storage",
            DependencyStatus.ABSENT,
            f"{type(exc).__name__}: {exc}",
            _elapsed(started),
        )
    finally:
        if owned and s3 is not None and hasattr(s3, "close"):
            with contextlib.suppress(Exception):
                s3.close()


def ensure_bucket(
    settings: S3Settings, *, client: Any | None = None, allow_create: bool = False
) -> None:
    """Verify bucket existence, creating it only when ``allow_create`` is True."""
    owned = client is None
    s3 = client or create_s3_client(settings)
    try:
        s3.head_bucket(Bucket=settings.bucket)
    except Exception as exc:  # noqa: BLE001
        if not allow_create or not is_not_found(exc):
            raise
        kwargs: dict[str, Any] = {"Bucket": settings.bucket}
        region = settings.region_name or getattr(getattr(s3, "meta", None), "region_name", None)
        if region and region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**kwargs)
    finally:
        if owned and hasattr(s3, "close"):
            with contextlib.suppress(Exception):
                s3.close()


def read_object_bytes(client: Any, *, bucket: str, key: str) -> bytes:
    response = client.get_object(Bucket=bucket, Key=key)
    with contextlib.closing(response["Body"]) as body:
        return body.read()


def put_object_bytes(
    client: Any,
    *,
    bucket: str,
    key: str,
    data: bytes,
    content_type: str = "application/octet-stream",
) -> None:
    """Upload raw bytes to ``bucket/key`` with explicit ``ContentType``."""
    client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)


def is_not_found(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    err = resp.get("Error") if isinstance(resp, dict) else None
    return isinstance(err, dict) and str(err.get("Code", "")) in _NOT_FOUND_CODES


def redact_presigned_url(url: str | None) -> str | None:
    if not url:
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            "signature=<redacted>" if parsed.query else "",
            "",
        )
    )


__all__ = [
    "S3Settings",
    "create_s3_client",
    "ensure_bucket",
    "is_not_found",
    "probe_s3",
    "put_object_bytes",
    "read_object_bytes",
    "redact_presigned_url",
]
