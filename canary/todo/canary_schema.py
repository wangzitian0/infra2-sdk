"""Code-side PostgreSQL enum and table contract for Canary Todo (`infra2_sdk.storage`)."""

from __future__ import annotations

from dataclasses import dataclass

CANARY_CAPABILITY_ENUM_NAME = "canary_todo_capability"
CANARY_CAPABILITY_LABELS: tuple[str, ...] = (
    "general",
    "postgres",
    "redis",
    "s3",
    "llm",
    "otel",
    "sso",
)
ENSURE_ENUM_SQL = (
    "DO $do$\n"
    "BEGIN\n"
    "  IF NOT EXISTS (\n"
    "    SELECT 1 FROM pg_type t\n"
    "    JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace\n"
    f"    WHERE t.typname = '{CANARY_CAPABILITY_ENUM_NAME}' AND n.nspname = 'public'\n"
    "  ) THEN\n"
    f"    CREATE TYPE {CANARY_CAPABILITY_ENUM_NAME} AS ENUM "
    f"({', '.join(repr(label) for label in CANARY_CAPABILITY_LABELS)});\n"
    "  END IF;\n"
    "END\n"
    "$do$;"
)
ENSURE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS todos (\n"
    "    id SERIAL PRIMARY KEY,\n"
    "    title TEXT NOT NULL,\n"
    "    completed BOOLEAN NOT NULL DEFAULT FALSE,\n"
    f"    capability {CANARY_CAPABILITY_ENUM_NAME} NOT NULL DEFAULT 'general',\n"
    "    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()\n"
    ");"
)
ENSURE_SCHEMA_SQL = f"{ENSURE_ENUM_SQL}\n{ENSURE_TABLE_SQL}"


@dataclass(frozen=True)
class _ColumnType:
    name: str
    enums: tuple[str, ...] = ()
    native_enum: bool = False


@dataclass(frozen=True)
class _Column:
    name: str
    type: _ColumnType


@dataclass(frozen=True)
class _Table:
    name: str
    columns: tuple[_Column, ...]


@dataclass(frozen=True)
class _Metadata:
    tables: dict[str, _Table]


METADATA = _Metadata(
    tables={
        "todos": _Table(
            name="todos",
            columns=(
                _Column(name="id", type=_ColumnType(name="integer")),
                _Column(name="title", type=_ColumnType(name="text")),
                _Column(name="completed", type=_ColumnType(name="boolean")),
                _Column(
                    name="capability",
                    type=_ColumnType(
                        name=CANARY_CAPABILITY_ENUM_NAME,
                        enums=CANARY_CAPABILITY_LABELS,
                        native_enum=True,
                    ),
                ),
                _Column(name="created_at", type=_ColumnType(name="timestamptz")),
            ),
        )
    }
)
