"""PostgreSQL settings, credential redaction, and reachability probes over psycopg."""

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


def _normalize_dsn(dsn: str) -> str:
    return _SQLALCHEMY_DRIVER_RE.sub("postgresql://", dsn.strip(), count=1)


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
        if not _normalize_dsn(self.dsn).startswith(("postgresql://", "postgres://")):
            raise ValueError("dsn must use PostgreSQL")
        if not 1 <= self.connect_timeout_seconds <= 60:
            raise ValueError("connect_timeout_seconds must be between 1 and 60")
        if not 1 <= self.statement_timeout_seconds <= 60:
            raise ValueError("statement_timeout_seconds must be between 1 and 60")
        if self.migrations_dsn is not None and not _normalize_dsn(self.migrations_dsn).startswith(
            ("postgresql://", "postgres://")
        ):
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
        return _normalize_dsn(self.dsn)


def probe_postgres(
    settings: PostgresSettings,
    *,
    connector: Callable[..., Any] | None = None,
    query: str = "SELECT 1",
) -> ProbeResult:
    """Execute a bounded PostgreSQL probe query with credential-redacted error reporting."""
    if not query.strip():
        raise ValueError("query must be non-empty")
    started = time.perf_counter()
    raw_conn = None
    try:
        connect = connector or require("psycopg", extra="postgres").connect
        kwargs: dict[str, Any] = {"connect_timeout": settings.connect_timeout_seconds}
        if connector is None:
            kwargs["options"] = (
                f"-c statement_timeout={int(settings.statement_timeout_seconds * 1000)}"
            )
        conn_or_cm = connect(settings.psycopg_dsn, **kwargs)
        if hasattr(conn_or_cm, "__enter__"):
            with conn_or_cm as conn:
                raw_conn = conn
                cur = conn.execute(query)
                if query.strip().upper().startswith("SELECT") and hasattr(cur, "fetchone"):
                    cur.fetchone()
        else:
            raw_conn = conn_or_cm
            cur = raw_conn.execute(query)
            if query.strip().upper().startswith("SELECT") and hasattr(cur, "fetchone"):
                cur.fetchone()
        detail = "SELECT 1 succeeded" if query == "SELECT 1" else f"{query.split()[0]} succeeded"
        return ProbeResult("database", DependencyStatus.PRESENT, detail, _elapsed(started))
    except Exception as exc:  # noqa: BLE001
        return ProbeResult(
            "database",
            DependencyStatus.ABSENT,
            f"{type(exc).__name__}: {redact_postgres_error(str(exc), settings)}",
            _elapsed(started),
        )
    finally:
        target = raw_conn if raw_conn is not None else locals().get("conn_or_cm")
        if target is not None and hasattr(target, "close"):
            with contextlib.suppress(Exception):
                target.close()


def redact_postgres_error(detail: str, settings: PostgresSettings) -> str:
    """Redact DSNs and embedded or query-string passwords from PostgreSQL error text."""
    redacted = detail
    eff_mig = _normalize_dsn(settings.migrations_dsn or settings.dsn)
    dsns = {d for d in (settings.dsn, settings.psycopg_dsn, eff_mig, settings.migrations_dsn) if d}
    for dsn in sorted(dsns, key=len, reverse=True):
        redacted = redacted.replace(dsn, "<redacted-postgres-dsn>")
    secrets: set[str] = set()
    for dsn in dsns:
        with contextlib.suppress(ValueError):
            parsed = urlsplit(dsn)
            if parsed.password:
                secrets.update((parsed.password, unquote(parsed.password)))
            if parsed.query:
                for k, v in parse_qsl(parsed.query, keep_blank_values=True):
                    if k.lower() == "password" and v:
                        secrets.update((v, unquote(v)))
                for part in parsed.query.split("&"):
                    rk, sep, rv = part.partition("=")
                    if sep and unquote(rk).lower() == "password" and rv:
                        secrets.update((rv, unquote(rv)))
    for secret in sorted(secrets, key=len, reverse=True):
        redacted = redacted.replace(secret, "<redacted>")
    return redacted


__all__ = [
    "PostgresSettings",
    "probe_postgres",
    "redact_postgres_error",
]
