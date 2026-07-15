"""Thin helpers over the open S3 API; object semantics remain application-owned."""

from __future__ import annotations

import contextlib
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from infra2_sdk.runtime._optional import require
from infra2_sdk.runtime.probes import DependencyStatus, ProbeResult

_BUCKET_RE = re.compile(r"\A[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]\Z")
_NOT_FOUND_CODES = frozenset({"404", "NoSuchBucket", "NoSuchKey", "NotFound"})


@dataclass(frozen=True)
class S3Settings:
    bucket: str
    endpoint_url: str | None = None
    access_key_id: str | None = None
    secret_access_key: str | None = field(default=None, repr=False)
    session_token: str | None = field(default=None, repr=False)
    region_name: str = "us-east-1"
    connect_timeout_seconds: float = 5.0
    read_timeout_seconds: float = 5.0
    addressing_style: str = "path"

    def __post_init__(self) -> None:
        if not _BUCKET_RE.match(self.bucket) or ".." in self.bucket:
            raise ValueError("bucket must be a lowercase S3-compatible name")
        if self.connect_timeout_seconds <= 0 or self.read_timeout_seconds <= 0:
            raise ValueError("S3 timeouts must be positive")
        if self.addressing_style not in {"auto", "path", "virtual"}:
            raise ValueError("addressing_style must be auto, path, or virtual")
        if bool(self.access_key_id) != bool(self.secret_access_key):
            raise ValueError("explicit S3 access key and secret key must be provided together")


def create_s3_client(settings: S3Settings, *, session: Any | None = None) -> Any:
    """Create and return the standard boto3 S3 client without performing I/O."""

    boto3 = require("boto3", extra="s3")
    botocore_config = require("botocore.config", extra="s3")
    owner = session or boto3
    return owner.client(
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
            s3={"addressing_style": settings.addressing_style},
        ),
    )


def probe_s3(settings: S3Settings, *, client: Any | None = None) -> ProbeResult:
    started = time.perf_counter()
    try:
        (client or create_s3_client(settings)).head_bucket(Bucket=settings.bucket)
        return ProbeResult(
            "object_storage",
            DependencyStatus.PRESENT,
            "bucket accessible",
            _elapsed(started),
        )
    except Exception as exc:  # noqa: BLE001 - a probe reports absence
        return ProbeResult(
            "object_storage",
            DependencyStatus.ABSENT,
            f"{type(exc).__name__}: {exc}",
            _elapsed(started),
        )


class S3Check:
    name = "object_storage"

    def __init__(self, settings: S3Settings, *, client: Any | None = None) -> None:
        self.settings = settings
        self.client = client

    def probe(self) -> ProbeResult:
        return probe_s3(self.settings, client=self.client)


def ensure_bucket(
    settings: S3Settings, *, client: Any | None = None, allow_create: bool = False
) -> None:
    """Assert a bucket exists, optionally creating it only when policy explicitly allows."""

    s3 = client or create_s3_client(settings)
    try:
        s3.head_bucket(Bucket=settings.bucket)
        return
    except Exception as exc:  # noqa: BLE001 - botocore is an optional dependency
        if not allow_create or not is_not_found(exc):
            raise
    kwargs: dict[str, Any] = {"Bucket": settings.bucket}
    if settings.region_name != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": settings.region_name}
    s3.create_bucket(**kwargs)


def read_object_bytes(client: Any, *, bucket: str, key: str) -> bytes:
    response = client.get_object(Bucket=bucket, Key=key)
    with contextlib.closing(response["Body"]) as body:
        return body.read()


def is_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    return isinstance(error, dict) and str(error.get("Code", "")) in _NOT_FOUND_CODES


def redact_presigned_url(url: str | None) -> str | None:
    """Remove bearer credentials from an S3 presigned URL before logging."""

    if not url:
        return url
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "<invalid-url>"
    query = "signature=<redacted>" if parsed.query else ""
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, ""))


def _elapsed(started: float) -> float:
    return (time.perf_counter() - started) * 1000
