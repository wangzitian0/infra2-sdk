"""PostgreSQL DSN normalization and reachability probes over psycopg."""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from infra2_sdk.runtime._optional import require
from infra2_sdk.runtime.health import DependencyStatus, ProbeResult, _elapsed

_SQLALCHEMY_DRIVER_RE = re.compile(r"\Apostgresql\+[a-zA-Z0-9_]+://")


@dataclass(frozen=True)
class PostgresSettings:
    dsn: str = field(repr=False)
    connect_timeout_seconds: int = 5
    statement_timeout_seconds: int = 5
    migrations_dsn: str | None = field(default=None, repr=False, kw_only=True)
    pool_size: int = field(default=5, kw_only=True)
    pool_max_overflow: int = field(default=10, kw_only=True)
    migration_lock_budget_seconds: float = field(default=15.0, kw_only=True)
    migration_lock_timeout_seconds: float = field(default=5.0, kw_only=True)
    migration_statement_timeout_seconds: float = field(default=30.0, kw_only=True)

    def __post_init__(self) -> None:
        normalized = normalize_postgres_dsn(self.dsn)
        if not normalized.startswith(("postgresql://", "postgres://")):
            raise ValueError("dsn must use PostgreSQL")
        if not 1 <= self.connect_timeout_seconds <= 60:
            raise ValueError("connect_timeout_seconds must be between 1 and 60")
        if not 1 <= self.statement_timeout_seconds <= 60:
            raise ValueError("statement_timeout_seconds must be between 1 and 60")
        if self.migrations_dsn is not None and not normalize_postgres_dsn(
            self.migrations_dsn
        ).startswith(("postgresql://", "postgres://")):
            raise ValueError("migrations_dsn must use PostgreSQL")
        if self.pool_size < 1:
            raise ValueError("pool_size must be at least 1")
        if self.pool_max_overflow < 0:
            raise ValueError("pool_max_overflow must be non-negative")
        if (
            self.migration_lock_budget_seconds <= 0
            or self.migration_lock_timeout_seconds <= 0
            or self.migration_statement_timeout_seconds <= 0
        ):
            raise ValueError("migration timeouts must be positive")
        if self.migration_lock_timeout_seconds >= self.migration_statement_timeout_seconds:
            raise ValueError(
                "migration_lock_timeout_seconds must be less than "
                "migration_statement_timeout_seconds"
            )

    @property
    def psycopg_dsn(self) -> str:
        return normalize_postgres_dsn(self.dsn)


def normalize_postgres_dsn(dsn: str) -> str:
    return _SQLALCHEMY_DRIVER_RE.sub("postgresql://", dsn.strip(), count=1)


def probe_postgres(
    settings: PostgresSettings,
    *,
    connector: Callable[..., Any] | None = None,
) -> ProbeResult:
    started = time.perf_counter()
    raw_conn = None
    try:
        connect = connector or require("psycopg", extra="postgres").connect
        connect_kwargs: dict[str, Any] = {
            "connect_timeout": settings.connect_timeout_seconds,
        }
        if connector is None:
            connect_kwargs["options"] = (
                f"-c statement_timeout={int(settings.statement_timeout_seconds * 1000)}"
            )
        conn_or_cm = connect(
            settings.psycopg_dsn,
            **connect_kwargs,
        )
        if hasattr(conn_or_cm, "__enter__"):
            with conn_or_cm as connection:
                raw_conn = connection
                connection.execute("SELECT 1").fetchone()
        else:
            raw_conn = conn_or_cm
            raw_conn.execute("SELECT 1").fetchone()
        return ProbeResult(
            "database",
            DependencyStatus.PRESENT,
            "SELECT 1 succeeded",
            _elapsed(started),
        )
    except Exception as exc:  # noqa: BLE001 - a probe reports absence
        return ProbeResult(
            "database",
            DependencyStatus.ABSENT,
            f"{type(exc).__name__}: {redact_postgres_error(str(exc), settings)}",
            _elapsed(started),
        )
    finally:
        target = (
            raw_conn if raw_conn is not None else (conn_or_cm if "conn_or_cm" in locals() else None)
        )
        if target is not None and hasattr(target, "close"):
            with contextlib.suppress(Exception):
                target.close()


def redact_postgres_error(detail: str, settings: PostgresSettings) -> str:
    """Redact DSN and password from PostgreSQL error strings."""
    redacted = detail
    effective_migrations = normalize_postgres_dsn(settings.migrations_dsn or settings.dsn)
    targets = {settings.dsn, settings.psycopg_dsn, effective_migrations}
    if settings.migrations_dsn:
        targets.add(settings.migrations_dsn)
    for dsn in sorted(targets, key=len, reverse=True):
        if dsn:
            redacted = redacted.replace(dsn, "<redacted-postgres-dsn>")
    secrets_to_redact: set[str] = set()
    for dsn_to_check in (
        settings.dsn,
        settings.psycopg_dsn,
        effective_migrations,
        *([settings.migrations_dsn] if settings.migrations_dsn else []),
    ):
        try:
            parsed = urlsplit(dsn_to_check)
            password = parsed.password
            query = parsed.query
        except ValueError:
            password = None
            query = ""
        if password:
            secrets_to_redact.add(password)
            secrets_to_redact.add(unquote(password))
        if query:
            for k, v in parse_qsl(query, keep_blank_values=True):
                if k.lower() == "password" and v:
                    secrets_to_redact.add(v)
                    secrets_to_redact.add(unquote(v))
            for part in query.split("&"):
                raw_k, sep, raw_v = part.partition("=")
                if sep and unquote(raw_k).lower() == "password" and raw_v:
                    secrets_to_redact.add(raw_v)
                    secrets_to_redact.add(unquote(raw_v))
    for secret in sorted(secrets_to_redact, key=len, reverse=True):
        redacted = redacted.replace(secret, "<redacted>")
    return redacted


__all__ = [
    "PostgresSettings",
    "probe_postgres",
    "redact_postgres_error",
]
