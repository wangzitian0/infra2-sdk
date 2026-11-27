"""PostgreSQL, Redis, S3 adapters, dependency probes, and deployment schema gate."""

from __future__ import annotations

import argparse as _argparse
import contextlib as _contextlib
import importlib as _importlib
import json as _json
import math as _math
import os as _os
import re as _re
import secrets as _secrets
import socket as _socket
import ssl as _ssl
import sys as _sys
import time as _time
from collections.abc import (
    Collection as _Collection,
)
from collections.abc import (
    Iterator as _Iterator,
)
from collections.abc import (
    Mapping as _Mapping,
)
from collections.abc import (
    Sequence as _Sequence,
)
from typing import Any as _Any
from typing import ClassVar as _ClassVar
from typing import Literal as _Literal
from typing import Self as _Self
from urllib.parse import unquote as _unquote
from urllib.parse import urlsplit as _urlsplit

from pydantic import Field as _Field
from pydantic import field_validator as _field_validator

from infra2_sdk._core import (
    ContractModel as _ContractModel,
)
from infra2_sdk._core import (
    load_dotted_symbol as _load_dotted_symbol,
)
from infra2_sdk._core import (
    redact_secrets as _redact_secrets,
)
from infra2_sdk._core import (
    redact_text as _redact_text,
)
from infra2_sdk._core import (
    redact_url as _redact_url,
)
from infra2_sdk._core import (
    redact_url_credentials as _redact_url_credentials,
)
from infra2_sdk._core import (
    require_optional as _require_optional,
)
from infra2_sdk._core import (
    validate_http_url as _validate_http_url,
)
from infra2_sdk.service import DependencyStatus as _DependencyStatus
from infra2_sdk.service import ProbeResult as _ProbeResult

_EXIT_OK = 0
_EXIT_BLOCKED = 1
_EXIT_NOT_EVALUATED = 3

_ROLLBACK_CLASS_A = "A"
_ROLLBACK_CLASS_C = "C"

_DEFAULT_TIMEOUT_SECONDS = 5.0
_PROBE_TABLE_RE = _re.compile(r"\A[A-Za-z_][A-Za-z0-9_]{0,62}\Z")
_RESP_CRLF = b"\r\n"
_RESP_MAX_LINE_BYTES = 65_536

_DB_ENUMS_SQL = """
SELECT n.nspname AS schema_name,
       t.typname AS enum_name,
       e.enumlabel AS enum_label
FROM pg_type t
JOIN pg_namespace n ON n.oid = t.typnamespace
JOIN pg_enum e ON e.enumtypid = t.oid
WHERE t.typtype = 'e'
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY n.nspname, t.typname, e.enumsortorder;
"""

_DB_TABLES_SQL = """
SELECT n.nspname AS table_schema,
       c.relname AS table_name,
       a.attname AS column_name,
       format_type(a.atttypid, a.atttypmod) AS udt_name
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid
WHERE c.relkind IN ('r', 'p')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND c.relname <> 'alembic_version'
  AND a.attnum > 0
  AND NOT a.attisdropped
ORDER BY n.nspname, c.relname, a.attnum;
"""

_DB_ENUM_TABLES_SQL = """
WITH RECURSIVE type_closure(base_enum_oid, type_oid, visited) AS (
    SELECT t.oid, t.oid, ARRAY[t.oid]
    FROM pg_type t
    JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE t.typtype = 'e'
      AND n.nspname NOT IN ('pg_catalog', 'information_schema')

    UNION ALL

    SELECT tc.base_enum_oid, next_oid, tc.visited || next_oid
    FROM type_closure tc
    CROSS JOIN LATERAL (
        SELECT arr.oid AS next_oid
        FROM pg_type arr
        WHERE arr.typelem = tc.type_oid
          AND arr.typtype = 'b'
          AND NOT (arr.oid = ANY(tc.visited))
        UNION
        SELECT dom.oid AS next_oid
        FROM pg_type dom
        WHERE dom.typtype = 'd'
          AND dom.typbasetype = tc.type_oid
          AND NOT (dom.oid = ANY(tc.visited))
        UNION
        SELECT rng.rngtypid AS next_oid
        FROM pg_range rng
        WHERE rng.rngsubtype = tc.type_oid
          AND NOT (rng.rngtypid = ANY(tc.visited))
        UNION
        SELECT mrng.rngmultitypid AS next_oid
        FROM pg_range mrng
        WHERE (mrng.rngsubtype = tc.type_oid OR mrng.rngtypid = tc.type_oid)
          AND mrng.rngmultitypid <> 0
          AND NOT (mrng.rngmultitypid = ANY(tc.visited))
        UNION
        SELECT comp.oid AS next_oid
        FROM pg_attribute ca
        JOIN pg_class cc ON cc.oid = ca.attrelid
        JOIN pg_type comp ON comp.oid = cc.reltype
        WHERE cc.relkind IN ('c', 'r', 'p')
          AND ca.attnum > 0
          AND NOT ca.attisdropped
          AND ca.atttypid = tc.type_oid
          AND NOT (comp.oid = ANY(tc.visited))
    ) sub
)
SELECT DISTINCT en.nspname AS enum_schema,
                et.typname AS enum_name,
                tn.nspname AS table_schema,
                c.relname AS table_name
FROM type_closure tc
JOIN pg_type et ON et.oid = tc.base_enum_oid
JOIN pg_namespace en ON en.oid = et.typnamespace
JOIN pg_attribute a ON a.atttypid = tc.type_oid
JOIN pg_class c ON c.oid = a.attrelid
JOIN pg_namespace tn ON tn.oid = c.relnamespace
WHERE a.attnum > 0
  AND NOT a.attisdropped
  AND c.relkind IN ('r', 'p')
  AND tn.nspname NOT IN ('pg_catalog', 'information_schema')
ORDER BY en.nspname, et.typname, tn.nspname, c.relname;
"""

_DISALLOWED_SQL_RE = _re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|UPSERT|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|"
    r"COPY|CALL|DO|EXECUTE|PREPARE|LOCK|VACUUM|ANALYZE|REINDEX|CLUSTER|COMMENT|"
    r"SET|RESET|BEGIN|COMMIT|ROLLBACK|SAVEPOINT|NEXTVAL|SETVAL|"
    r"PG_TERMINATE_BACKEND|PG_CANCEL_BACKEND|SET_CONFIG)\b",
    _re.IGNORECASE,
)
_LINE_COMMENT_RE = _re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = _re.compile(r"/\*.*?\*/", _re.DOTALL)
_STRING_LITERAL_RE = _re.compile(r"'(?:''|[^'])*'")
_FALSY_SCALAR_STRINGS = frozenset({"false", "f", "0", "no", "none", ""})


class _GateNotEvaluated(RuntimeError):
    """Raised when the schema gate cannot evaluate database or migration state."""


class _UrlConnectionSettings(_ContractModel):
    _DEFAULT_ENV_VAR: _ClassVar[str]
    url: str = _Field(repr=False)
    connect_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS

    @_field_validator("connect_timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: float) -> float:
        if not _math.isfinite(value) or value <= 0:
            raise ValueError("connect_timeout_seconds must be positive")
        return float(value)

    @classmethod
    def from_env(
        cls,
        environ: _Mapping[str, str] | None = None,
        *,
        url_env_var: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> _Self:
        """Build connection settings from an environment mapping."""
        env = _os.environ if environ is None else environ
        key = url_env_var or cls._DEFAULT_ENV_VAR
        raw = env.get(key, "").strip()
        if not raw:
            raise ValueError(f"{key} is required")
        return cls(url=raw, connect_timeout_seconds=timeout_seconds)


class PostgresSettings(_UrlConnectionSettings):
    """PostgreSQL connection configuration."""

    _DEFAULT_ENV_VAR: _ClassVar[str] = "DATABASE_URL"

    @_field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return _normalize_postgres_dsn(value)


def _normalize_postgres_dsn(dsn: str) -> str:
    """Normalize postgresql+<driver>:// or postgres:// DSNs into postgresql:// libpq form."""
    text = dsn.strip()
    if not text:
        raise ValueError("PostgreSQL DSN must not be empty")
    parsed = _urlsplit(text)
    scheme = parsed.scheme.lower()
    base = scheme.split("+", 1)[0]
    if base not in {"postgresql", "postgres"}:
        raise ValueError(f"unsupported PostgreSQL DSN scheme {parsed.scheme!r}")
    try:
        _ = parsed.port
    except ValueError:
        raise ValueError("PostgreSQL DSN port must be between 1 and 65535") from None
    has_unix_host = any(
        k.lower() == "host" and v.strip()
        for k, v in (pair.partition("=")[::2] for pair in parsed.query.split("&") if "=" in pair)
    )
    if not parsed.hostname and not has_unix_host:
        raise ValueError("PostgreSQL DSN must include a hostname")
    rebuilt = f"postgresql://{parsed.netloc}{parsed.path}"
    if parsed.query:
        rebuilt += f"?{parsed.query}"
    if parsed.fragment:
        rebuilt += f"#{parsed.fragment}"
    return rebuilt


def redact_postgres_error(exc: BaseException, dsn: str | None = None) -> str:
    """Format an exception message while redacting embedded PostgreSQL credentials."""
    detail = f"{type(exc).__name__}: {exc}"
    return _redact_secrets(
        _redact_url_credentials(detail, dsn, placeholder="postgresql://[REDACTED]")
    )


def probe_postgres(
    url: str,
    *,
    connect_timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    name: str = "postgres",
    verify_write: bool = False,
    probe_table: str = "infra2_sdk_probe",
) -> _ProbeResult:
    """Verify PostgreSQL connectivity (`SELECT 1`) and optional temp-table write round-trip."""
    if not _math.isfinite(connect_timeout) or connect_timeout <= 0:
        raise ValueError("connect_timeout must be positive")
    if not _PROBE_TABLE_RE.fullmatch(probe_table):
        raise ValueError("probe_table must be a valid SQL identifier")
    psycopg = _require_optional("psycopg", extra="postgres")
    started = _time.perf_counter()
    try:
        dsn = _normalize_postgres_dsn(url)
        timeout_arg = max(1, int(_math.ceil(connect_timeout)))
        timeout_ms = timeout_arg * 1000
        with (
            psycopg.connect(
                dsn,
                connect_timeout=timeout_arg,
                options=f"-c statement_timeout={timeout_ms}",
            ) as conn,
            conn.cursor() as cur,
        ):
            cur.execute("SELECT 1")
            row = cur.fetchone()
            if not row or row[0] != 1:
                return _ProbeResult(
                    name=name,
                    status=_DependencyStatus.ABSENT,
                    detail="SELECT 1 returned unexpected result",
                    duration_ms=(_time.perf_counter() - started) * 1000,
                )
            if verify_write:
                cur.execute("SET LOCAL transaction_read_only = off")
                cur.execute(
                    f"CREATE TEMP TABLE IF NOT EXISTS {probe_table} "
                    "(id TEXT PRIMARY KEY, title TEXT NOT NULL)"
                )
                cur.execute(
                    f"INSERT INTO {probe_table} (id, title) VALUES ('probe-1', 'ok') "
                    "ON CONFLICT (id) DO UPDATE SET title = EXCLUDED.title"
                )
                cur.execute(f"SELECT title FROM {probe_table} WHERE id = 'probe-1'")
                w_row = cur.fetchone()
                cur.execute(f"DELETE FROM {probe_table} WHERE id = 'probe-1'")
                if not w_row or w_row[0] != "ok":
                    return _ProbeResult(
                        name=name,
                        status=_DependencyStatus.ABSENT,
                        detail=f"temp-table round-trip returned {w_row!r}",
                        duration_ms=(_time.perf_counter() - started) * 1000,
                    )
        return _ProbeResult(
            name=name,
            status=_DependencyStatus.PRESENT,
            detail="ok (write verified)" if verify_write else "ok",
            duration_ms=(_time.perf_counter() - started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        return _ProbeResult(
            name=name,
            status=_DependencyStatus.ABSENT,
            detail=redact_postgres_error(exc, url),
            duration_ms=(_time.perf_counter() - started) * 1000,
        )


def _validate_redis_url(value: str) -> tuple[str, int, str, str, int, bool]:
    text = value.strip()
    if not text:
        raise ValueError("Redis URL must not be empty")
    parsed = _urlsplit(text)
    scheme = parsed.scheme.lower()
    if scheme not in {"redis", "rediss"}:
        raise ValueError(f"unsupported Redis URL scheme {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("Redis URL must include a hostname")
    try:
        port = parsed.port if parsed.port is not None else 6379
    except ValueError:
        raise ValueError("Redis URL port must be between 1 and 65535") from None
    db_raw = parsed.path.lstrip("/")
    db_index = 0
    if db_raw:
        if not db_raw.isdigit():
            raise ValueError(f"invalid Redis database index {db_raw!r}")
        db_index = int(db_raw)
    return (
        parsed.hostname,
        port,
        _unquote(parsed.username or ""),
        _unquote(parsed.password or ""),
        db_index,
        scheme == "rediss",
    )


class RedisSettings(_UrlConnectionSettings):
    """Redis connection configuration."""

    _DEFAULT_ENV_VAR: _ClassVar[str] = "REDIS_URL"
    tls_verify: bool = True

    @_field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        _validate_redis_url(value)
        return value.strip()


def redact_redis_error(exc: BaseException, url: str | None = None) -> str:
    """Format an exception message while redacting embedded Redis credentials."""
    detail = f"{type(exc).__name__}: {exc}"
    return _redact_secrets(_redact_url_credentials(detail, url, placeholder="redis://[REDACTED]"))


def _encode_resp(*parts: str) -> bytes:
    buf = [f"*{len(parts)}\r\n".encode("ascii")]
    for part in parts:
        raw = part.encode("utf-8")
        buf.append(f"${len(raw)}\r\n".encode("ascii"))
        buf.append(raw)
        buf.append(_RESP_CRLF)
    return b"".join(buf)


class _RespReader:
    def __init__(self, sock: _socket.socket) -> None:
        self._sock = sock
        self._buf = bytearray()

    def _read_line(self) -> bytes:
        while True:
            idx = self._buf.find(_RESP_CRLF)
            if idx != -1:
                line = bytes(self._buf[:idx])
                del self._buf[: idx + 2]
                return line
            if len(self._buf) > _RESP_MAX_LINE_BYTES:
                raise ValueError("RESP reply line exceeded maximum length")
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

    def read_reply(self) -> _Any:
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
    key_prefix: str = "sdk:probe:",
    connect_timeout: float = _DEFAULT_TIMEOUT_SECONDS,
    tls_verify: bool = True,
    write_probe: bool = True,
    name: str = "redis",
) -> _ProbeResult:
    """Verify Redis connectivity with PING and optional ephemeral SETEX/GET/DEL round-trip."""
    if not _math.isfinite(connect_timeout) or connect_timeout <= 0:
        raise ValueError("connect_timeout must be positive")
    started = _time.perf_counter()
    deadline = _time.monotonic() + connect_timeout
    raw_sock: _socket.socket | None = None
    sock: _socket.socket | None = None
    try:
        host, port, username, password, db_index, use_tls = _validate_redis_url(url)
        raw_sock = _socket.create_connection((host, port), timeout=connect_timeout)
        if use_tls:
            ctx = _ssl.create_default_context()
            if not tls_verify:
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
            sock = ctx.wrap_socket(raw_sock, server_hostname=host)
            raw_sock = None
        else:
            sock = raw_sock
            raw_sock = None

        reader = _RespReader(sock)

        def cmd(*args: str) -> _Any:
            assert sock is not None
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Redis probe exceeded timeout budget")
            sock.settimeout(remaining)
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

        if write_probe:
            key = f"{key_prefix}{_secrets.token_hex(8)}"
            token = _secrets.token_hex(12)
            cmd("SETEX", key, "30", token)
            try:
                fetched = cmd("GET", key)
                if fetched != token:
                    raise RuntimeError(f"Redis GET returned {fetched!r}, expected {token!r}")
            finally:
                with _contextlib.suppress(Exception):
                    cmd("DEL", key)

        return _ProbeResult(
            name=name,
            status=_DependencyStatus.PRESENT,
            detail="ok",
            duration_ms=(_time.perf_counter() - started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        return _ProbeResult(
            name=name,
            status=_DependencyStatus.ABSENT,
            detail=redact_redis_error(exc, url),
            duration_ms=(_time.perf_counter() - started) * 1000,
        )
    finally:
        for handle in (sock, raw_sock):
            if handle is not None:
                with _contextlib.suppress(OSError):
                    handle.close()


def _validate_readonly_sql(sql: str) -> str:
    """Validate that a precondition statement is a single read-only SELECT/WITH query."""
    if "$$" in sql:
        raise ValueError("precondition SQL forbids dollar-quoted blocks")
    placeholder_tokens: list[str] = []

    def _stash_literal(match: _re.Match[str]) -> str:
        placeholder_tokens.append(match.group(0))
        return f"__SQL_LIT_{len(placeholder_tokens) - 1}__"

    stashed = _STRING_LITERAL_RE.sub(_stash_literal, sql)
    stripped = _BLOCK_COMMENT_RE.sub(" ", _LINE_COMMENT_RE.sub(" ", stashed)).strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if not stripped:
        raise ValueError("precondition SQL must not be empty")
    if ";" in stripped:
        raise ValueError("precondition SQL must contain a single statement")
    upper = stripped.lstrip().upper()
    if not (upper.startswith("SELECT ") or upper.startswith("WITH ")):
        raise ValueError("precondition SQL must begin with SELECT or WITH")
    if match := _DISALLOWED_SQL_RE.search(stripped):
        raise ValueError(f"precondition SQL forbids keyword {match.group(0).upper()!r}")
    restored = stripped
    for idx, literal in enumerate(placeholder_tokens):
        restored = restored.replace(f"__SQL_LIT_{idx}__", literal, 1)
    return restored


def _fetch_db_enums(cur: _Any) -> dict[tuple[str, str], tuple[str, ...]]:
    cur.execute(_DB_ENUMS_SQL)
    grouped: dict[tuple[str, str], list[str]] = {}
    for schema_name, enum_name, enum_label in cur.fetchall():
        grouped.setdefault((str(schema_name), str(enum_name)), []).append(str(enum_label))
    return {k: tuple(v) for k, v in grouped.items()}


def _fetch_db_columns(cur: _Any) -> dict[tuple[str, str], dict[str, str]]:
    cur.execute(_DB_TABLES_SQL)
    tables: dict[tuple[str, str], dict[str, str]] = {}
    for schema_name, table_name, column_name, udt_name in cur.fetchall():
        tables.setdefault((str(schema_name), str(table_name)), {})[str(column_name)] = str(udt_name)
    return tables


def _fetch_db_enum_tables(cur: _Any) -> dict[tuple[str, str], frozenset[tuple[str, str]]]:
    cur.execute(_DB_ENUM_TABLES_SQL)
    mapping: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for enum_schema, enum_name, table_schema, table_name in cur.fetchall():
        mapping.setdefault((str(enum_schema), str(enum_name)), set()).add(
            (str(table_schema), str(table_name))
        )
    return {k: frozenset(v) for k, v in mapping.items()}


def _run_preconditions(cur: _Any, queries: _Sequence[str]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    for raw in queries:
        stmt = _validate_readonly_sql(raw)
        cur.execute(stmt)
        rows = cur.fetchall()
        if not rows:
            failures.append(f"precondition returned 0 rows: {stmt}")
            continue
        if len(rows) == 1 and len(rows[0]) == 1:
            scalar = rows[0][0]
            if (
                scalar is None
                or scalar is False
                or str(scalar).strip().lower() in _FALSY_SCALAR_STRINGS
            ):
                failures.append(f"precondition evaluated false ({scalar!r}): {stmt}")
    return (not failures, failures)


def _check_alembic_pending(
    cur: _Any,
    *,
    alembic_config_path: str | None = None,
    alembic_script_location: str | None = None,
) -> bool:
    if not alembic_config_path and not alembic_script_location:
        return False
    try:
        alembic_cfg_mod = _require_optional("alembic.config", extra="postgres")
        alembic_script_mod = _require_optional("alembic.script", extra="postgres")
    except RuntimeError:
        raise RuntimeError(
            "alembic is required in the application environment to evaluate pending migrations"
        ) from None

    if alembic_config_path:
        cfg = alembic_cfg_mod.Config(alembic_config_path)
        if alembic_script_location:
            cfg.set_main_option("script_location", alembic_script_location)
        script = alembic_script_mod.ScriptDirectory.from_config(cfg)
    else:
        script = alembic_script_mod.ScriptDirectory(alembic_script_location)

    expected_heads = tuple(sorted(script.get_heads()))
    cur.execute("SELECT to_regclass('public.alembic_version')")
    reg = cur.fetchone()
    if not reg or reg[0] is None:
        return bool(expected_heads)
    cur.execute("SELECT version_num FROM public.alembic_version")
    current_heads = tuple(sorted(str(row[0]) for row in cur.fetchall() if row and row[0]))
    return expected_heads != current_heads


def _unwrap_native_enum(column_type: _Any) -> _Any | None:
    """Unwrap Enum, ARRAY(Enum), or TypeDecorator(Enum) when backed by PostgreSQL native ENUM."""
    for candidate in (
        column_type,
        getattr(column_type, "item_type", None),
        getattr(column_type, "impl_instance", None),
    ):
        if candidate is None:
            continue
        enums = getattr(candidate, "enums", None)
        enum_name = getattr(candidate, "name", None)
        if (
            isinstance(enums, (list, tuple))
            and enums
            and enum_name
            and getattr(candidate, "native_enum", True)
        ):
            return candidate
    return None


def _iter_metadata_enum_columns(
    metadata: _Any, *, default_schema: str = "public"
) -> _Iterator[tuple[str, str, str, str, tuple[str, ...]]]:
    if metadata is None or not hasattr(metadata, "tables"):
        raise ValueError("metadata with a .tables mapping is required")
    for table_key, table in metadata.tables.items():
        table_schema = str(getattr(table, "schema", None) or default_schema)
        table_name = str(getattr(table, "name", None) or str(table_key).rsplit(".", 1)[-1])
        for col in table.columns:
            enum_type = _unwrap_native_enum(col.type)
            if enum_type is None:
                continue
            enum_schema = str(getattr(enum_type, "schema", None) or table_schema)
            labels = tuple(str(label) for label in enum_type.enums)
            yield table_schema, table_name, enum_schema, str(enum_type.name), labels


def _normalize_expected_tables(
    expected_tables: _Mapping[tuple[str, str], _Mapping[str, str]] | None,
    metadata: _Any = None,
    *,
    default_schema: str = "public",
) -> dict[tuple[str, str], dict[str, str]]:
    out: dict[tuple[str, str], dict[str, str]] = {}
    if metadata is not None and hasattr(metadata, "tables"):
        for table_key, table in metadata.tables.items():
            schema_name = str(getattr(table, "schema", None) or default_schema)
            table_name = str(getattr(table, "name", None) or str(table_key).rsplit(".", 1)[-1])
            out[(schema_name, table_name)] = {
                str(getattr(col, "name", None) or str(col)): str(
                    getattr(col.type, "name", None) or type(col.type).__name__
                )
                for col in table.columns
            }
    if expected_tables:
        for table_key, cols in expected_tables.items():
            schema_name, table_name = str(table_key[0]), str(table_key[1])
            out[(schema_name, table_name)] = {str(k): str(v) for k, v in cols.items()}
    return out


class _EnumSchemaSpec(_ContractModel):
    schema_name: str = "public"
    name: str
    labels: tuple[str, ...]

    @_field_validator("schema_name", "name")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("schema_name and name must be non-empty")
        return text

    @_field_validator("labels")
    @classmethod
    def _validate_labels(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("enum labels must not be empty")
        if len(value) != len(set(value)):
            raise ValueError("enum labels must be unique")
        return value


_EnumDiscrepancyKind = _Literal[
    "missing_type",
    "extra_type",
    "additive_labels",
    "destructive_labels",
    "order_mismatch",
]


class _EnumDiscrepancy(_ContractModel):
    schema_name: str = "public"
    name: str
    kind: _EnumDiscrepancyKind
    expected: tuple[str, ...] = ()
    actual: tuple[str, ...] = ()
    missing_in_db: tuple[str, ...] = ()
    missing_in_code: tuple[str, ...] = ()


class _TableColumn(_ContractModel):
    schema_name: str = "public"
    table_name: str
    column_name: str
    data_type: str = ""


class _SchemaDiffReport(_ContractModel):
    additive_columns: tuple[_TableColumn, ...] = ()
    dropped_columns: tuple[_TableColumn, ...] = ()
    new_tables: tuple[tuple[str, str], ...] = ()
    dropped_tables: tuple[tuple[str, str], ...] = ()
    enum_discrepancies: tuple[_EnumDiscrepancy, ...] = ()
    migrations_pending: bool = False
    preconditions_passed: bool = True
    precondition_failures: tuple[str, ...] = ()
    rollback_class: _Literal["A", "C"] = _ROLLBACK_CLASS_A

    @property
    def is_blocking(self) -> bool:
        return (
            self.rollback_class == _ROLLBACK_CLASS_C
            or not self.preconditions_passed
            or (not self.migrations_pending and bool(self.enum_discrepancies))
        )


def _enums_from_metadata(
    metadata: _Any, *, default_schema: str = "public"
) -> tuple[_EnumSchemaSpec, ...]:
    specs: dict[tuple[str, str], tuple[str, ...]] = {}
    for _t_schema, _t_name, enum_schema, enum_name, labels in _iter_metadata_enum_columns(
        metadata, default_schema=default_schema
    ):
        key = (enum_schema, enum_name)
        if key in specs and set(specs[key]) != set(labels):
            raise ValueError(f"conflicting enum definitions for {enum_schema}.{enum_name}")
        specs.setdefault(key, labels)
    return tuple(
        _EnumSchemaSpec(schema_name=k[0], name=k[1], labels=v) for k, v in sorted(specs.items())
    )


def _enum_tables_from_metadata(
    metadata: _Any, *, default_schema: str = "public"
) -> dict[tuple[str, str], frozenset[tuple[str, str]]]:
    refs: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for t_schema, t_name, enum_schema, enum_name, _labels in _iter_metadata_enum_columns(
        metadata, default_schema=default_schema
    ):
        refs.setdefault((enum_schema, enum_name), set()).add((t_schema, t_name))
    return {k: frozenset(v) for k, v in refs.items()}


def _check_enums(
    expected: _Sequence[_EnumSchemaSpec],
    actual: _Mapping[tuple[str, str], _Sequence[str]],
) -> tuple[_EnumDiscrepancy, ...]:
    out: list[_EnumDiscrepancy] = []
    expected_keys: set[tuple[str, str]] = set()
    for spec in expected:
        key = (spec.schema_name, spec.name)
        expected_keys.add(key)
        if key not in actual:
            out.append(
                _EnumDiscrepancy(
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
            kind: _EnumDiscrepancyKind = "additive_labels"
        elif not missing_in_code and not missing_in_db:
            kind = "order_mismatch"
        else:
            kind = "destructive_labels"
        out.append(
            _EnumDiscrepancy(
                schema_name=spec.schema_name,
                name=spec.name,
                kind=kind,
                expected=spec.labels,
                actual=live,
                missing_in_db=missing_in_db,
                missing_in_code=missing_in_code,
            )
        )
    for key in sorted(set(actual) - expected_keys):
        live = tuple(str(item) for item in actual[key])
        out.append(
            _EnumDiscrepancy(
                schema_name=key[0],
                name=key[1],
                kind="extra_type",
                expected=(),
                actual=live,
                missing_in_db=(),
                missing_in_code=live,
            )
        )
    return tuple(out)


def _classify_rollback(
    *,
    dropped_columns: _Sequence[_TableColumn] = (),
    new_tables: _Collection[tuple[str, str]] = (),
    dropped_tables: _Collection[tuple[str, str]] = (),
    enum_discrepancies: _Sequence[_EnumDiscrepancy] = (),
    enum_tables: _Mapping[tuple[str, str], _Collection[tuple[str, str]]] | None = None,
    code_tables: _Collection[tuple[str, str]] | None = None,
    migrations_pending: bool = False,
) -> _Literal["A", "C"]:
    dropped_table_set = {(str(s), str(t)) for s, t in dropped_tables}
    new_table_set = {(str(s), str(t)) for s, t in new_tables}
    code_table_set = {(str(s), str(t)) for s, t in code_tables} if code_tables is not None else None
    if dropped_table_set:
        return _ROLLBACK_CLASS_C
    for col in dropped_columns:
        if (col.schema_name, col.table_name) not in dropped_table_set:
            return _ROLLBACK_CLASS_C
    enum_ref_map = enum_tables or {}
    for diff in enum_discrepancies:
        if diff.kind == "additive_labels":
            continue
        refs = {(str(s), str(t)) for s, t in enum_ref_map.get((diff.schema_name, diff.name), ())}
        if diff.kind == "missing_type":
            if (refs and refs <= new_table_set) or (not refs and new_table_set):
                continue
            return _ROLLBACK_CLASS_C
        if diff.kind == "extra_type":
            if not migrations_pending or not refs:
                return _ROLLBACK_CLASS_C
            if code_table_set is not None and not (refs & code_table_set):
                continue
            if refs <= dropped_table_set:
                continue
            return _ROLLBACK_CLASS_C
        if refs and refs <= dropped_table_set:
            continue
        return _ROLLBACK_CLASS_C
    return _ROLLBACK_CLASS_A


def _check_schema(
    database_url: str,
    *,
    metadata: _Any = None,
    expected_tables: _Mapping[tuple[str, str], _Mapping[str, str]] | None = None,
    expected_enums: _Sequence[_EnumSchemaSpec] = (),
    alembic_config_path: str | None = None,
    alembic_script_location: str | None = None,
    preconditions: _Sequence[str] = (),
    default_schema: str = "public",
    connect_timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> _SchemaDiffReport:
    psycopg = _require_optional("psycopg", extra="postgres")
    dsn = _normalize_postgres_dsn(database_url)
    target_tables = _normalize_expected_tables(
        expected_tables, metadata=metadata, default_schema=default_schema
    )
    merged_enums = list(expected_enums)
    code_enum_tables: dict[tuple[str, str], frozenset[tuple[str, str]]] = {}
    if metadata is not None:
        by_key = {(e.schema_name, e.name): e for e in merged_enums}
        for spec in _enums_from_metadata(metadata, default_schema=default_schema):
            by_key.setdefault((spec.schema_name, spec.name), spec)
        merged_enums = list(by_key.values())
        code_enum_tables = _enum_tables_from_metadata(metadata, default_schema=default_schema)

    timeout_arg = max(1, int(_math.ceil(connect_timeout)))
    opts = f"-c statement_timeout={timeout_arg * 1000} -c default_transaction_read_only=on"
    try:
        conn = psycopg.connect(dsn, connect_timeout=timeout_arg, options=opts)
    except Exception as exc:  # noqa: BLE001
        raise _GateNotEvaluated(redact_postgres_error(exc, database_url)) from exc

    try:
        with conn, conn.cursor() as cur:
            live_enums = _fetch_db_enums(cur)
            live_tables = _fetch_db_columns(cur)
            db_enum_tables = _fetch_db_enum_tables(cur)
            pre_ok, pre_failures = _run_preconditions(cur, preconditions)
            pending = _check_alembic_pending(
                cur,
                alembic_config_path=alembic_config_path,
                alembic_script_location=alembic_script_location,
            )
    except Exception as exc:  # noqa: BLE001
        raise _GateNotEvaluated(redact_postgres_error(exc, database_url)) from exc

    additive_cols: list[_TableColumn] = []
    dropped_cols: list[_TableColumn] = []
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
                    _TableColumn(
                        schema_name=table_key[0],
                        table_name=table_key[1],
                        column_name=cname,
                        data_type=exp_cols[cname],
                    )
                )
            for cname in sorted(set(act_cols) - set(exp_cols)):
                dropped_cols.append(
                    _TableColumn(
                        schema_name=table_key[0],
                        table_name=table_key[1],
                        column_name=cname,
                        data_type=act_cols[cname],
                    )
                )

    enum_diffs: tuple[_EnumDiscrepancy, ...] = ()
    if merged_enums or metadata is not None:
        enum_diffs = _check_enums(merged_enums, live_enums)

    combined_enum_tables: dict[tuple[str, str], set[tuple[str, str]]] = {
        k: set(v) for k, v in db_enum_tables.items()
    }
    for key, tables in code_enum_tables.items():
        combined_enum_tables.setdefault(key, set()).update(tables)

    rollback_class = _classify_rollback(
        dropped_columns=dropped_cols,
        new_tables=new_tables,
        dropped_tables=dropped_tables,
        enum_discrepancies=enum_diffs,
        enum_tables=combined_enum_tables,
        code_tables=set(target_tables) if target_tables else None,
        migrations_pending=pending,
    )
    return _SchemaDiffReport(
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


class S3Settings(_ContractModel):
    """S3-compatible object storage configuration (path-style addressing enabled by default)."""

    endpoint_url: str
    bucket: str
    access_key_id: str
    secret_access_key: str = _Field(repr=False)
    region: str = "us-east-1"
    use_path_style: bool = True
    public_endpoint_url: str | None = None
    connect_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    read_timeout_seconds: float = 10.0

    @_field_validator("endpoint_url")
    @classmethod
    def _validate_endpoint(cls, value: str) -> str:
        text = value.strip()
        _validate_http_url(text, label="S3 endpoint_url")
        return text

    @_field_validator("public_endpoint_url", mode="before")
    @classmethod
    def _validate_public_endpoint(cls, value: _Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise TypeError("S3 public_endpoint_url must be a string")
        text = value.strip()
        if not text:
            return None
        _validate_http_url(text, label="S3 public_endpoint_url")
        return text

    @_field_validator("bucket", "access_key_id", "secret_access_key", "region")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("S3 setting value must not be empty")
        return text

    @_field_validator("connect_timeout_seconds", "read_timeout_seconds")
    @classmethod
    def _validate_timeouts(cls, value: float) -> float:
        if not _math.isfinite(value) or value <= 0:
            raise ValueError("timeout must be positive")
        return float(value)

    @classmethod
    def from_env(
        cls,
        environ: _Mapping[str, str] | None = None,
        *,
        bucket_env: str = "S3_BUCKET",
    ) -> S3Settings:
        """Build S3Settings from shared_env.yaml S3 variables and optional S3_USE_PATH_STYLE."""
        env = _os.environ if environ is None else environ
        style_raw = env.get("S3_USE_PATH_STYLE", "true").strip().lower()
        return cls(
            endpoint_url=env.get("S3_ENDPOINT", ""),
            bucket=env.get(bucket_env, ""),
            access_key_id=env.get("S3_ACCESS_KEY", ""),
            secret_access_key=env.get("S3_SECRET_KEY", ""),
            region=env.get("S3_REGION", "").strip() or "us-east-1",
            use_path_style=style_raw not in {"0", "false", "no"},
            public_endpoint_url=env.get("S3_PUBLIC_ENDPOINT", "").strip() or None,
        )


def create_s3_client(settings: S3Settings) -> _Any:
    """Create a configured boto3 S3 client."""
    boto3 = _require_optional("boto3", extra="s3")
    botocore_config = _require_optional("botocore.config", extra="s3")
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
    if not isinstance(resp, _Mapping):
        return False
    code = str(resp.get("Error", {}).get("Code", ""))
    status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return status == 404 or code in {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}


def ensure_bucket(client: _Any, bucket: str, region: str = "us-east-1") -> None:
    """Create the bucket idempotently if head_bucket reports missing."""
    try:
        client.head_bucket(Bucket=bucket)
        return
    except Exception as exc:
        if not is_not_found(exc):
            raise
    kwargs: dict[str, _Any] = {"Bucket": bucket}
    if region and region != "us-east-1":
        kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
    try:
        client.create_bucket(**kwargs)
    except Exception as exc:
        resp = getattr(exc, "response", None)
        code = str(resp.get("Error", {}).get("Code", "")) if isinstance(resp, _Mapping) else ""
        if code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
            raise


def put_object_bytes(
    client: _Any,
    bucket: str,
    key: str,
    data: bytes,
    *,
    content_type: str = "application/octet-stream",
) -> str:
    """Upload raw bytes to S3 and return the unquoted ETag."""
    resp = client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
    return str(resp.get("ETag", "")).strip('"')


def read_object_bytes(client: _Any, bucket: str, key: str) -> bytes:
    """Download an S3 object body as bytes and close its stream."""
    resp = client.get_object(Bucket=bucket, Key=key)
    body = resp["Body"]
    try:
        return bytes(body.read())
    finally:
        close_fn = getattr(body, "close", None)
        if callable(close_fn):
            with _contextlib.suppress(Exception):
                close_fn()


def redact_presigned_url(url: str) -> str:
    """Strip presigned query tokens from an S3 URL for safe logging."""
    return _redact_url(url)


def presign_get_url(
    settings: S3Settings,
    key: str,
    *,
    bucket: str | None = None,
    expires_in: int = 3600,
    public: bool = True,
    response_content_disposition: str | None = None,
    response_content_type: str | None = None,
    client: _Any | None = None,
) -> str:
    """Generate a presigned GET URL using the public endpoint when configured and enabled."""
    cleaned_key = key.strip()
    if not cleaned_key:
        raise ValueError("key must not be empty")
    if (
        isinstance(expires_in, bool)
        or not isinstance(expires_in, int)
        or not (1 <= expires_in <= 604800)
    ):
        raise ValueError("expires_in must be an integer between 1 and 604800")

    target_endpoint = (
        settings.public_endpoint_url
        if public and settings.public_endpoint_url
        else settings.endpoint_url
    )
    target_bucket = (bucket or settings.bucket).strip()
    if not target_bucket:
        raise ValueError("bucket must not be empty")

    params: dict[str, _Any] = {"Bucket": target_bucket, "Key": cleaned_key}
    if response_content_disposition is not None:
        params["ResponseContentDisposition"] = response_content_disposition
    if response_content_type is not None:
        params["ResponseContentType"] = response_content_type

    owned = client is None
    s3_client = client or create_s3_client(
        settings.model_copy(update={"endpoint_url": target_endpoint})
    )
    try:
        return str(
            s3_client.generate_presigned_url(
                "get_object",
                Params=params,
                ExpiresIn=expires_in,
            )
        )
    finally:
        if owned and hasattr(s3_client, "close") and callable(s3_client.close):
            with _contextlib.suppress(Exception):
                s3_client.close()


def probe_s3(
    settings: S3Settings,
    *,
    key_prefix: str = "sdk/probe/",
    name: str = "s3",
    round_trip: bool = False,
    client: _Any | None = None,
) -> _ProbeResult:
    """Verify S3 bucket reachability and optional write/read/delete round-trip."""
    started = _time.perf_counter()
    owned = client is None
    s3_client: _Any | None = client
    try:
        if s3_client is None:
            s3_client = create_s3_client(settings)
        s3_client.head_bucket(Bucket=settings.bucket)
        if round_trip:
            key = f"{key_prefix}{_secrets.token_hex(8)}.txt"
            payload = _secrets.token_hex(8).encode("utf-8")
            put_object_bytes(s3_client, settings.bucket, key, payload, content_type="text/plain")
            try:
                got = read_object_bytes(s3_client, settings.bucket, key)
            finally:
                with _contextlib.suppress(Exception):
                    s3_client.delete_object(Bucket=settings.bucket, Key=key)
            if got != payload:
                raise RuntimeError("S3 round-trip content mismatch")
        return _ProbeResult(
            name=name,
            status=_DependencyStatus.PRESENT,
            detail="ok",
            duration_ms=(_time.perf_counter() - started) * 1000,
        )
    except Exception as exc:  # noqa: BLE001
        detail = _redact_text(
            f"{type(exc).__name__}: {exc}",
            extra_secrets=(settings.access_key_id, settings.secret_access_key),
        )
        return _ProbeResult(
            name=name,
            status=_DependencyStatus.ABSENT,
            detail=detail,
            duration_ms=(_time.perf_counter() - started) * 1000,
        )
    finally:
        if owned and s3_client is not None:
            close_fn = getattr(s3_client, "close", None)
            if callable(close_fn):
                with _contextlib.suppress(Exception):
                    close_fn()


def check_schema_cli(argv: _Sequence[str] | None = None) -> int:
    """CLI entrypoint: ``python -m infra2_sdk.storage check-schema ...``."""
    parser = _argparse.ArgumentParser(prog="python -m infra2_sdk.storage")
    sub = parser.add_subparsers(dest="command", required=True)
    gate = sub.add_parser("check-schema", help="Run the PostgreSQL schema & migration gate.")
    gate.add_argument(
        "--database-url",
        "--db-url",
        dest="database_url",
        default=_os.environ.get("DATABASE_URL", ""),
    )
    gate.add_argument(
        "--import",
        action="append",
        dest="import_modules",
        default=[],
        help="Dotted module name to import before loading --metadata",
    )
    gate.add_argument("--metadata", help="Dotted module:ATTR pointing to MetaData or enum stub")
    gate.add_argument("--label", default="", help="Optional service label for output")
    gate.add_argument("--alembic-config")
    gate.add_argument("--alembic-script-location")
    gate.add_argument("--precondition", action="append", default=[])
    gate.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.database_url:
        print("DATABASE_URL is required", file=_sys.stderr)
        return _EXIT_NOT_EVALUATED
    try:
        if args.import_modules:
            if "" not in _sys.path:
                _sys.path.insert(0, "")
            for mod_name in args.import_modules:
                _importlib.import_module(mod_name)
        metadata = _load_dotted_symbol(args.metadata) if args.metadata else None
        report = _check_schema(
            args.database_url,
            metadata=metadata,
            alembic_config_path=args.alembic_config,
            alembic_script_location=args.alembic_script_location,
            preconditions=args.precondition,
        )
    except (_GateNotEvaluated, ValueError, ImportError, AttributeError) as exc:
        print(str(exc), file=_sys.stderr)
        return _EXIT_NOT_EVALUATED
    payload = report.to_dict()
    if args.label:
        payload["label"] = args.label
    if args.as_json:
        print(_json.dumps(payload, sort_keys=True))
    else:
        prefix = f"[{args.label}] " if args.label else ""
        print(
            f"{prefix}ROLLBACK_CLASS: {report.rollback_class} pending={report.migrations_pending}"
        )
    return _EXIT_BLOCKED if report.is_blocking else _EXIT_OK


_main = check_schema_cli


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = (
    "PostgresSettings",
    "RedisSettings",
    "S3Settings",
    "check_schema_cli",
    "create_s3_client",
    "ensure_bucket",
    "is_not_found",
    "presign_get_url",
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
    raise SystemExit(check_schema_cli())
