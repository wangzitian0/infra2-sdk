"""PostgreSQL, Redis, S3 adapters, dependency probes, and deployment schema gate."""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import secrets
import socket
import ssl
import sys
import time
from collections.abc import Collection, Mapping, Sequence
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import field_validator

from infra2_sdk._core import (
    ContractModel,
    redact_secrets,
    redact_url,
    redact_url_credentials,
    require_optional,
    validate_http_url,
)
from infra2_sdk._schema_gate import (
    check_alembic_pending,
    fetch_db_columns,
    fetch_db_enum_tables,
    fetch_db_enums,
    normalize_expected_tables,
    run_preconditions,
)
from infra2_sdk.service import DependencyStatus, ProbeResult

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_NOT_EVALUATED = 3

ROLLBACK_CLASS_A = "A"
ROLLBACK_CLASS_C = "C"

_DEFAULT_TIMEOUT_SECONDS = 5.0
_MAX_PORT = 65535
_RESP_CRLF = b"\r\n"


class GateNotEvaluated(RuntimeError):
    """Raised when the schema gate cannot evaluate database or migration state."""


class PostgresSettings(ContractModel):
    """PostgreSQL connection configuration."""

    url: str
    connect_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return normalize_postgres_dsn(value)

    @field_validator("connect_timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        return float(value)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        url_env_var: str = "DATABASE_URL",
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> PostgresSettings:
        """Build PostgresSettings from an environment mapping."""
        env = os.environ if environ is None else environ
        raw = env.get(url_env_var, "").strip()
        if not raw:
            raise ValueError(f"{url_env_var} is required")
        return cls(url=raw, connect_timeout_seconds=timeout_seconds)


def normalize_postgres_dsn(dsn: str) -> str:
    """Normalize postgresql+<driver>:// or postgres:// DSNs into postgresql:// libpq form."""
    text = dsn.strip()
    if not text:
        raise ValueError("PostgreSQL DSN must not be empty")
    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    base = scheme.split("+", 1)[0]
    if base not in {"postgresql", "postgres"}:
        raise ValueError(f"unsupported PostgreSQL DSN scheme {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("PostgreSQL DSN must include a hostname")
    if parsed.port is not None and not 1 <= parsed.port <= _MAX_PORT:
        raise ValueError("PostgreSQL DSN port must be between 1 and 65535")
    return urlunsplit(("postgresql", parsed.netloc, parsed.path, parsed.query, parsed.fragment))


def redact_postgres_error(exc: BaseException, dsn: str | None = None) -> str:
    """Format an exception message while redacting embedded PostgreSQL credentials."""
    detail = f"{type(exc).__name__}: {exc}"
    return redact_secrets(
        redact_url_credentials(detail, dsn, placeholder="postgresql://[REDACTED]")
    )


def probe_postgres(
    url: str,
    *,
    connect_timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    name: str = "postgres",
) -> ProbeResult:
    """Verify PostgreSQL connectivity and query execution with SELECT 1."""
    if not math.isfinite(connect_timeout) or connect_timeout <= 0:
        raise ValueError("connect_timeout must be positive")
    psycopg = require_optional("psycopg", extra="postgres")
    started = time.perf_counter()
    try:
        dsn = normalize_postgres_dsn(url)
        timeout_arg = max(1, int(math.ceil(connect_timeout)))
        with psycopg.connect(dsn, connect_timeout=timeout_arg) as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            row = cur.fetchone()
        if not row or row[0] != 1:
            return ProbeResult(
                name=name,
                status=DependencyStatus.ABSENT,
                detail="SELECT 1 returned unexpected result",
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        return ProbeResult(
            name=name,
            status=DependencyStatus.PRESENT,
            detail="ok",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            name=name,
            status=DependencyStatus.ABSENT,
            detail=redact_postgres_error(exc, url),
            duration_ms=(time.perf_counter() - started) * 1000,
        )


def _validate_redis_url(value: str) -> tuple[str, int, str, str, int, bool]:
    text = value.strip()
    if not text:
        raise ValueError("Redis URL must not be empty")
    parsed = urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme not in {"redis", "rediss"}:
        raise ValueError(f"unsupported Redis URL scheme {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("Redis URL must include a hostname")
    port = parsed.port if parsed.port is not None else 6379
    if not 1 <= port <= _MAX_PORT:
        raise ValueError("Redis URL port must be between 1 and 65535")
    db_raw = parsed.path.lstrip("/")
    db_index = 0
    if db_raw:
        if not db_raw.isdigit():
            raise ValueError(f"invalid Redis database index {db_raw!r}")
        db_index = int(db_raw)
    return (
        parsed.hostname,
        port,
        parsed.username or "",
        parsed.password or "",
        db_index,
        scheme == "rediss",
    )


class RedisSettings(ContractModel):
    """Redis connection configuration."""

    url: str
    connect_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    tls_verify: bool = True

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        _validate_redis_url(value)
        return value.strip()

    @field_validator("connect_timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        return float(value)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        url_env_var: str = "REDIS_URL",
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> RedisSettings:
        """Build RedisSettings from an environment mapping."""
        env = os.environ if environ is None else environ
        raw = env.get(url_env_var, "").strip()
        if not raw:
            raise ValueError(f"{url_env_var} is required")
        return cls(url=raw, connect_timeout_seconds=timeout_seconds)


def redact_redis_error(exc: BaseException, url: str | None = None) -> str:
    """Format an exception message while redacting embedded Redis credentials."""
    detail = f"{type(exc).__name__}: {exc}"
    return redact_secrets(redact_url_credentials(detail, url, placeholder="redis://[REDACTED]"))


def _encode_resp(*parts: str) -> bytes:
    buf = [f"*{len(parts)}\r\n".encode("ascii")]
    for part in parts:
        raw = part.encode("utf-8")
        buf.append(f"${len(raw)}\r\n".encode("ascii"))
        buf.append(raw)
        buf.append(_RESP_CRLF)
    return b"".join(buf)


class _RespReader:
    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()

    def _read_line(self) -> bytes:
        while True:
            idx = self._buf.find(_RESP_CRLF)
            if idx != -1:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 2]
                return line
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("unexpected EOF from Redis")
            self._buf.extend(chunk)

    def _read_exact(self, length: int) -> bytes:
        while len(self._buf) < length + 2:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("unexpected EOF from Redis bulk payload")
            self._buf.extend(chunk)
        data = bytes(self._buf[:length])
        del self._buf[: length + 2]
        return data

    def read_reply(self) -> Any:
        line = self._read_line()
        prefix, payload = line[:1], line[1:].decode("utf-8", errors="replace")
        if prefix == b"+":
            return payload
        if prefix == b"-":
            raise RuntimeError(f"Redis error: {payload}")
        if prefix == b":":
            return int(payload)
        if prefix == b"$":
            size = int(payload)
            return None if size < 0 else self._read_exact(size).decode("utf-8", errors="replace")
        raise RuntimeError(f"unsupported RESP reply prefix {prefix!r}")


def probe_redis(
    url: str,
    *,
    key_prefix: str = "canary:probe:",
    connect_timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    tls_verify: bool = True,
    name: str = "redis",
) -> ProbeResult:
    """Verify Redis connectivity with PING plus ephemeral SETEX/GET/DEL round-trip."""
    if not math.isfinite(connect_timeout) or connect_timeout <= 0:
        raise ValueError("connect_timeout must be positive")
    started = time.perf_counter()
    sock: socket.socket | None = None
    try:
        host, port, username, password, db_index, use_tls = _validate_redis_url(url)
        raw_sock = socket.create_connection((host, port), timeout=connect_timeout)
        if use_tls:
            ctx = ssl.create_default_context()
            if not tls_verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            sock = ctx.wrap_socket(raw_sock, server_hostname=host)
            sock.settimeout(connect_timeout)
        else:
            sock = raw_sock
            sock.settimeout(connect_timeout)

        reader = _RespReader(sock)

        def cmd(*args: str) -> Any:
            assert sock is not None
            sock.sendall(_encode_resp(*args))
            return reader.read_reply()

        if password:
            try:
                if username:
                    cmd("AUTH", username, password)
                else:
                    cmd("AUTH", password)
            except Exception as exc:
                raise RuntimeError(f"AUTH failed: {exc}") from None
        if db_index:
            cmd("SELECT", str(db_index))
        if cmd("PING") != "PONG":
            raise RuntimeError("Redis PING did not return PONG")

        key = f"{key_prefix}{secrets.token_hex(8)}"
        token = secrets.token_hex(12)
        cmd("SETEX", key, "30", token)
        fetched = cmd("GET", key)
        cmd("DEL", key)
        if fetched != token:
            raise RuntimeError(f"Redis GET returned {fetched!r}, expected {token!r}")
        return ProbeResult(
            name=name,
            status=DependencyStatus.PRESENT,
            detail="ok",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            name=name,
            status=DependencyStatus.ABSENT,
            detail=redact_redis_error(exc, url),
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    finally:
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.close()


class EnumSchemaSpec(ContractModel):
    """Expected PostgreSQL enum type definition."""

    schema_name: str = "public"
    name: str
    labels: tuple[str, ...]

    @field_validator("schema_name", "name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("schema_name and name must be non-empty")
        return text

    @field_validator("labels")
    @classmethod
    def _validate_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("enum labels must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("enum labels must be unique")
        return value


class EnumDiscrepancy(ContractModel):
    """Difference between expected and live PostgreSQL enum definitions."""

    schema_name: str = "public"
    name: str
    kind: Literal["missing_type", "additive_labels", "destructive_labels", "order_mismatch"]
    expected: tuple[str, ...] = ()
    actual: tuple[str, ...] = ()
    missing_in_db: tuple[str, ...] = ()
    missing_in_code: tuple[str, ...] = ()


class TableColumn(ContractModel):
    """Qualified database column descriptor."""

    schema_name: str = "public"
    table_name: str
    column_name: str
    data_type: str = ""


class SchemaDiffReport(ContractModel):
    """Result of evaluating live PostgreSQL schema against target contracts."""

    additive_columns: tuple[TableColumn, ...] = ()
    dropped_columns: tuple[TableColumn, ...] = ()
    new_tables: tuple[tuple[str, str], ...] = ()
    dropped_tables: tuple[tuple[str, str], ...] = ()
    enum_discrepancies: tuple[EnumDiscrepancy, ...] = ()
    migrations_pending: bool = False
    preconditions_passed: bool = True
    precondition_failures: tuple[str, ...] = ()
    rollback_class: Literal["A", "C"] = ROLLBACK_CLASS_A

    @property
    def is_blocking(self) -> bool:
        return (
            self.rollback_class == ROLLBACK_CLASS_C
            or self.migrations_pending
            or not self.preconditions_passed
        )


def enums_from_metadata(
    metadata: Any, *, default_schema: str = "public"
) -> tuple[EnumSchemaSpec, ...]:
    """Extract EnumSchemaSpec definitions from SQLAlchemy MetaData."""
    if metadata is None or not hasattr(metadata, "tables"):
        raise ValueError("metadata with a .tables mapping is required")
    specs: dict[tuple[str, str], tuple[str, ...]] = {}
    for table in metadata.tables.values():
        table_schema = str(getattr(table, "schema", None) or default_schema)
        for col in table.columns:
            col_type = col.type
            enums = getattr(col_type, "enums", None)
            enum_name = getattr(col_type, "name", None)
            if not enums or not enum_name:
                continue
            schema_name = str(getattr(col_type, "schema", None) or table_schema)
            key = (schema_name, str(enum_name))
            labels = tuple(str(label) for label in enums)
            if key in specs and specs[key] != labels:
                raise ValueError(f"conflicting enum definitions for {schema_name}.{enum_name}")
            specs[key] = labels
    return tuple(
        EnumSchemaSpec(schema_name=k[0], name=k[1], labels=v) for k, v in sorted(specs.items())
    )


def check_enums(
    expected: Sequence[EnumSchemaSpec],
    actual: Mapping[tuple[str, str], Sequence[str]],
) -> tuple[EnumDiscrepancy, ...]:
    """Compare expected EnumSchemaSpec objects against live database enum labels."""
    out: list[EnumDiscrepancy] = []
    for spec in expected:
        key = (spec.schema_name, spec.name)
        if key not in actual:
            out.append(
                EnumDiscrepancy(
                    schema_name=spec.schema_name,
                    name=spec.name,
                    kind="missing_type",
                    expected=spec.labels,
                    actual=(),
                    missing_in_db=spec.labels,
                    missing_in_code=(),
                )
            )
            continue
        live = tuple(str(item) for item in actual[key])
        if live == spec.labels:
            continue
        live_set = set(live)
        exp_set = set(spec.labels)
        missing_in_db = tuple(label for label in spec.labels if label not in live_set)
        missing_in_code = tuple(label for label in live if label not in exp_set)
        if not missing_in_code and [label for label in spec.labels if label in live_set] == list(
            live
        ):
            kind: Literal[
                "missing_type", "additive_labels", "destructive_labels", "order_mismatch"
            ] = "additive_labels"
        elif not missing_in_code and not missing_in_db:
            kind = "order_mismatch"
        else:
            kind = "destructive_labels"
        out.append(
            EnumDiscrepancy(
                schema_name=spec.schema_name,
                name=spec.name,
                kind=kind,
                expected=spec.labels,
                actual=live,
                missing_in_db=missing_in_db,
                missing_in_code=missing_in_code,
            )
        )
    return tuple(out)


def classify_rollback(
    *,
    dropped_columns: Sequence[TableColumn] = (),
    new_tables: Collection[tuple[str, str]] = (),
    dropped_tables: Collection[tuple[str, str]] = (),
    enum_discrepancies: Sequence[EnumDiscrepancy] = (),
    enum_tables: Mapping[tuple[str, str], Collection[tuple[str, str]]] | None = None,
) -> Literal["A", "C"]:
    """Classify migration rollback safety as A (online-reversible) or C (exclusive/unsafe)."""
    dropped_table_set = {(str(s), str(t)) for s, t in dropped_tables}
    new_table_set = {(str(s), str(t)) for s, t in new_tables}
    if dropped_table_set:
        return ROLLBACK_CLASS_C
    for col in dropped_columns:
        if (col.schema_name, col.table_name) not in dropped_table_set:
            return ROLLBACK_CLASS_C
    enum_ref_map = enum_tables or {}
    for diff in enum_discrepancies:
        if diff.kind == "additive_labels":
            continue
        refs = {(str(s), str(t)) for s, t in enum_ref_map.get((diff.schema_name, diff.name), ())}
        if diff.kind == "missing_type":
            if not refs or refs <= new_table_set:
                continue
            return ROLLBACK_CLASS_C
        if refs and refs <= dropped_table_set:
            continue
        return ROLLBACK_CLASS_C
    return ROLLBACK_CLASS_A


def check_schema(
    database_url: str,
    *,
    metadata: Any = None,
    expected_tables: Mapping[tuple[str, str], Mapping[str, str]] | None = None,
    expected_enums: Sequence[EnumSchemaSpec] = (),
    alembic_config_path: str | None = None,
    alembic_script_location: str | None = None,
    preconditions: Sequence[str] = (),
    default_schema: str = "public",
    connect_timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> SchemaDiffReport:
    """Evaluate database schema compatibility and preconditions on one PostgreSQL connection."""
    psycopg = require_optional("psycopg", extra="postgres")
    dsn = normalize_postgres_dsn(database_url)
    target_tables = (
        normalize_expected_tables(expected_tables, default_schema=default_schema)
        if expected_tables is not None
        else {}
    )
    merged_enums = list(expected_enums)
    if metadata is not None:
        by_key = {(e.schema_name, e.name): e for e in merged_enums}
        for spec in enums_from_metadata(metadata, default_schema=default_schema):
            by_key.setdefault((spec.schema_name, spec.name), spec)
        merged_enums = list(by_key.values())

    timeout_arg = max(1, int(math.ceil(connect_timeout)))
    try:
        conn = psycopg.connect(dsn, connect_timeout=timeout_arg)
    except Exception as exc:  # noqa: BLE001
        raise GateNotEvaluated(redact_postgres_error(exc, database_url)) from exc

    with conn:
        with conn.cursor() as cur:
            live_enums = fetch_db_enums(cur)
            live_tables = fetch_db_columns(cur)
            enum_tables = fetch_db_enum_tables(cur)
            pre_ok, pre_failures = run_preconditions(cur, preconditions)
        pending, _, _ = check_alembic_pending(
            conn,
            alembic_config_path=alembic_config_path,
            alembic_script_location=alembic_script_location,
        )

    additive_cols: list[TableColumn] = []
    dropped_cols: list[TableColumn] = []
    new_tables: list[tuple[str, str]] = []
    dropped_tables: list[tuple[str, str]] = []

    if target_tables:
        new_tables = sorted(set(target_tables) - set(live_tables))
        dropped_tables = sorted(set(live_tables) - set(target_tables))
        for table_key in sorted(set(target_tables) & set(live_tables)):
            exp_cols = target_tables[table_key]
            act_cols = live_tables[table_key]
            for cname in sorted(set(exp_cols) - set(act_cols)):
                additive_cols.append(
                    TableColumn(
                        schema_name=table_key[0],
                        table_name=table_key[1],
                        column_name=cname,
                        data_type=exp_cols[cname],
                    )
                )
            for cname in sorted(set(act_cols) - set(exp_cols)):
                dropped_cols.append(
                    TableColumn(
                        schema_name=table_key[0],
                        table_name=table_key[1],
                        column_name=cname,
                        data_type=act_cols[cname],
                    )
                )

    enum_diffs = check_enums(merged_enums, live_enums)
    rollback_class = classify_rollback(
        dropped_columns=dropped_cols,
        new_tables=new_tables,
        dropped_tables=dropped_tables,
        enum_discrepancies=enum_diffs,
        enum_tables=enum_tables,
    )
    return SchemaDiffReport(
        additive_columns=tuple(additive_cols),
        dropped_columns=tuple(dropped_cols),
        new_tables=tuple(new_tables),
        dropped_tables=tuple(dropped_tables),
        enum_discrepancies=enum_diffs,
        migrations_pending=pending,
        preconditions_passed=pre_ok,
        precondition_failures=tuple(pre_failures),
        rollback_class=rollback_class,
    )


class S3Settings(ContractModel):
    """S3-compatible object storage configuration."""

    endpoint_url: str
    bucket: str
    access_key_id: str
    secret_access_key: str
    region: str = "us-east-1"
    use_path_style: bool = True
    connect_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    read_timeout_seconds: float = 10.0

    @field_validator("endpoint_url")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        text = value.strip()
        validate_http_url(text, label="S3 endpoint_url")
        return text

    @field_validator("bucket", "access_key_id", "secret_access_key", "region")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("S3 setting value must not be empty")
        return text

    @field_validator("connect_timeout_seconds", "read_timeout_seconds")
    @classmethod
    def _validate_timeouts(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("timeout must be positive")
        return float(value)

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        bucket_env: str = "S3_BUCKET",
    ) -> S3Settings:
        """Build S3Settings from canonical shared_env.yaml environment variables."""
        env = os.environ if environ is None else environ
        style_raw = env.get("S3_USE_PATH_STYLE", "true").strip().lower()
        return cls(
            endpoint_url=env.get("S3_ENDPOINT", ""),
            bucket=env.get(bucket_env, ""),
            access_key_id=env.get("S3_ACCESS_KEY", ""),
            secret_access_key=env.get("S3_SECRET_KEY", ""),
            region=env.get("S3_REGION", "").strip() or "us-east-1",
            use_path_style=style_raw not in {"0", "false", "no"},
        )


def create_s3_client(settings: S3Settings) -> Any:
    """Create a configured boto3 S3 client."""
    boto3 = require_optional("boto3", extra="s3")
    botocore_config = require_optional("botocore.config", extra="s3")
    cfg = botocore_config.Config(
        region_name=settings.region,
        signature_version="s3v4",
        connect_timeout=settings.connect_timeout_seconds,
        read_timeout=settings.read_timeout_seconds,
        s3={"addressing_style": "path" if settings.use_path_style else "virtual"},
    )
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        region_name=settings.region,
        config=cfg,
    )


def is_not_found(exc: BaseException) -> bool:
    """Return True if a botocore/S3 exception represents a 404 not-found error."""
    resp = getattr(exc, "response", None)
    if not isinstance(resp, Mapping):
        return False
    code = str(resp.get("Error", {}).get("Code", ""))
    status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status == 404 or code in {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}


def ensure_bucket(client: Any, bucket: str, region: str = "us-east-1") -> None:
    """Create the bucket idempotently if head_bucket reports missing."""
    try:
        client.head_bucket(Bucket=bucket)
        return
    except Exception as exc:
        if not is_not_found(exc):
            raise
    kwargs: dict[str, Any] = {"Bucket": bucket}
    if region and region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    try:
        client.create_bucket(**kwargs)
    except Exception as exc:
        resp = getattr(exc, "response", None)
        code = str(resp.get("Error", {}).get("Code", "")) if isinstance(resp, Mapping) else ""
        if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            raise


def put_object_bytes(
    client: Any,
    bucket: str,
    key: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload raw bytes to S3 and return the unquoted ETag."""
    resp = client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
    return str(resp.get("ETag", "")).strip('"')


def read_object_bytes(client: Any, bucket: str, key: str) -> bytes:
    """Download an S3 object body as bytes."""
    resp = client.get_object(Bucket=bucket, Key=key)
    return bytes(resp["Body"].read())


def redact_presigned_url(url: str) -> str:
    """Strip presigned query tokens from an S3 URL for safe logging."""
    return redact_url(url)


def probe_s3(
    settings: S3Settings,
    *,
    key_prefix: str = "canary/probe/",
    name: str = "s3",
    round_trip: bool = False,
    client: Any | None = None,
) -> ProbeResult:
    """Verify S3 bucket reachability and optional write/read/delete round-trip."""
    started = time.perf_counter()
    try:
        s3_client = client if client is not None else create_s3_client(settings)
        s3_client.head_bucket(Bucket=settings.bucket)
        if round_trip:
            key = f"{key_prefix}{secrets.token_hex(8)}.txt"
            payload = secrets.token_hex(8).encode("utf-8")
            put_object_bytes(s3_client, settings.bucket, key, payload, content_type="text/plain")
            got = read_object_bytes(s3_client, settings.bucket, key)
            s3_client.delete_object(Bucket=settings.bucket, Key=key)
            if got != payload:
                raise RuntimeError("S3 round-trip content mismatch")
        return ProbeResult(
            name=name,
            status=DependencyStatus.PRESENT,
            detail="ok",
            duration_ms=(time.perf_counter() - started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        detail = redact_secrets(f"{type(exc).__name__}: {exc}")
        detail = detail.replace(settings.access_key_id, "[REDACTED]").replace(
            settings.secret_access_key, "[REDACTED]"
        )
        return ProbeResult(
            name=name,
            status=DependencyStatus.ABSENT,
            detail=detail,
            duration_ms=(time.perf_counter() - started) * 1000,
        )


def _load_symbol(spec: str) -> Any:
    import importlib

    module_name, sep, attr_path = spec.partition(":")
    if not sep or not module_name or not attr_path:
        raise ValueError(f"invalid dotted symbol {spec!r}; expected module:attr")
    if "" not in sys.path:
        sys.path.insert(0, "")
    obj: Any = importlib.import_module(module_name)
    for part in attr_path.split("."):
        obj = getattr(obj, part)
    return obj


def _cli(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m infra2_sdk.storage")
    sub = parser.add_subparsers(dest="command", required=True)
    gate = sub.add_parser("check-schema", help="Run the PostgreSQL schema & migration gate.")
    gate.add_argument(
        "--database-url",
        "--db-url",
        dest="database_url",
        default=os.environ.get("DATABASE_URL", ""),
    )
    gate.add_argument("--metadata", help="Dotted module:ATTR pointing to MetaData or enum stub")
    gate.add_argument("--label", default="", help="Optional service label for output")
    gate.add_argument("--alembic-config")
    gate.add_argument("--alembic-script-location")
    gate.add_argument("--precondition", action="append", default=[])
    gate.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "check-schema":
        if not args.database_url:
            print("DATABASE_URL is required", file=sys.stderr)
            return EXIT_NOT_EVALUATED
        metadata = _load_symbol(args.metadata) if args.metadata else None
        try:
            report = check_schema(
                args.database_url,
                metadata=metadata,
                alembic_config_path=args.alembic_config,
                alembic_script_location=args.alembic_script_location,
                preconditions=args.precondition,
            )
        except GateNotEvaluated as exc:
            print(str(exc), file=sys.stderr)
            return EXIT_NOT_EVALUATED
        payload = report.to_dict()
        if args.label:
            payload["label"] = args.label
        if args.as_json:
            print(json.dumps(payload, sort_keys=True))
        else:
            prefix = f"[{args.label}] " if args.label else ""
            print(
                f"{prefix}ROLLBACK_CLASS: {report.rollback_class} "
                f"pending={report.migrations_pending}"
            )
        return EXIT_BLOCKED if report.is_blocking else EXIT_OK
    return EXIT_NOT_EVALUATED


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for schema gate inspection."""
    return _cli(argv)


__all__ = (
    "EXIT_BLOCKED",
    "EXIT_NOT_EVALUATED",
    "EXIT_OK",
    "EnumDiscrepancy",
    "EnumSchemaSpec",
    "GateNotEvaluated",
    "PostgresSettings",
    "ROLLBACK_CLASS_A",
    "ROLLBACK_CLASS_C",
    "RedisSettings",
    "S3Settings",
    "SchemaDiffReport",
    "TableColumn",
    "check_enums",
    "check_schema",
    "classify_rollback",
    "create_s3_client",
    "ensure_bucket",
    "enums_from_metadata",
    "is_not_found",
    "main",
    "normalize_postgres_dsn",
    "probe_postgres",
    "probe_redis",
    "probe_s3",
    "put_object_bytes",
    "read_object_bytes",
    "redact_postgres_error",
    "redact_presigned_url",
    "redact_redis_error",
)

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
