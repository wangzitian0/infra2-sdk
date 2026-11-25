"""Code-side PostgreSQL enum contract for Canary Todo (`infra2_sdk.runtime.schema_check`)."""

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


@dataclass(frozen=True)
class _EnumType:
    name: str
    enums: tuple[str, ...]
    native_enum: bool = True


@dataclass(frozen=True)
class _Column:
    type: _EnumType
    name: str = "capability"


@dataclass(frozen=True)
class _Table:
    columns: tuple[_Column, ...]


@dataclass(frozen=True)
class _Metadata:
    tables: dict[str, _Table]


METADATA = _Metadata(
    tables={
        "todos": _Table(
            columns=(
                _Column(
                    type=_EnumType(
                        name=CANARY_CAPABILITY_ENUM_NAME,
                        enums=CANARY_CAPABILITY_LABELS,
                        native_enum=True,
                    )
                ),
            )
        )
    }
)
