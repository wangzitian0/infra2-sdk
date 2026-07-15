"""PostgreSQL DSN normalization and reachability probes over psycopg."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from infra2_sdk.runtime._optional import require
from infra2_sdk.runtime.probes import DependencyStatus, ProbeResult

_SQLALCHEMY_DRIVER_RE = re.compile(r"\Apostgresql\+[a-zA-Z0-9_]+://")


@dataclass(frozen=True)
class PostgresSettings:
    dsn: str = field(repr=False)
    connect_timeout_seconds: int = 5

    def __post_init__(self) -> None:
        normalized = normalize_postgres_dsn(self.dsn)
        if not normalized.startswith(("postgresql://", "postgres://")):
            raise ValueError("dsn must use PostgreSQL")
        if not 1 <= self.connect_timeout_seconds <= 60:
            raise ValueError("connect_timeout_seconds must be between 1 and 60")

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
    try:
        connect = connector or require("psycopg", extra="postgres").connect
        with connect(
            settings.psycopg_dsn,
            connect_timeout=settings.connect_timeout_seconds,
        ) as connection:
            connection.execute("SELECT 1").fetchone()
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
            f"{type(exc).__name__}: {exc}",
            _elapsed(started),
        )


class PostgresCheck:
    name = "database"

    def __init__(
        self,
        settings: PostgresSettings,
        *,
        connector: Callable[..., Any] | None = None,
    ) -> None:
        self.settings = settings
        self.connector = connector

    async def probe(self) -> ProbeResult:
        return await asyncio.to_thread(probe_postgres, self.settings, connector=self.connector)


def _elapsed(started: float) -> float:
    return (time.perf_counter() - started) * 1000
