"""Internal PostgreSQL schema inspection and SQL safety helpers for infra2_sdk.storage."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from infra2_sdk._core import require_optional

DB_ENUMS_SQL = """
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

DB_TABLES_SQL = """
SELECT table_schema,
       table_name,
       column_name,
       udt_name
FROM information_schema.columns
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY table_schema, table_name, ordinal_position;
"""

DB_ENUM_TABLES_SQL = """
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

_DISALLOWED_SQL_RE = re.compile(
    r"\b(?:INSERT|UPDATE|DELETE|MERGE|UPSERT|CREATE|ALTER|DROP|TRUNCATE|GRANT|REVOKE|"
    r"COPY|CALL|DO|EXECUTE|PREPARE|LOCK|VACUUM|ANALYZE|REINDEX|CLUSTER|COMMENT|"
    r"SET|RESET|BEGIN|COMMIT|ROLLBACK|SAVEPOINT)\b",
    re.IGNORECASE,
)
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)
_STRING_LITERAL_RE = re.compile(r"'(?:''|[^'])*'")


def validate_readonly_sql(sql: str) -> str:
    """Validate that a precondition statement is a single read-only SELECT/WITH query."""
    cleaned = _BLOCK_COMMENT_RE.sub(" ", _LINE_COMMENT_RE.sub(" ", sql)).strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    if not cleaned:
        raise ValueError("precondition SQL must not be empty")
    unquoted = _STRING_LITERAL_RE.sub("''", cleaned)
    if ";" in unquoted:
        raise ValueError("precondition SQL must contain a single statement")
    upper = unquoted.lstrip().upper()
    if not (upper.startswith("SELECT ") or upper.startswith("WITH ")):
        raise ValueError("precondition SQL must begin with SELECT or WITH")
    if match := _DISALLOWED_SQL_RE.search(unquoted):
        raise ValueError(f"precondition SQL forbids keyword {match.group(0).upper()!r}")
    return cleaned


def fetch_db_enums(cur: Any) -> dict[tuple[str, str], tuple[str, ...]]:
    """Query PostgreSQL user enums using an open cursor."""
    cur.execute(DB_ENUMS_SQL)
    grouped: dict[tuple[str, str], list[str]] = {}
    for schema_name, enum_name, enum_label in cur.fetchall():
        grouped.setdefault((str(schema_name), str(enum_name)), []).append(str(enum_label))
    return {k: tuple(v) for k, v in grouped.items()}


def fetch_db_columns(cur: Any) -> dict[tuple[str, str], dict[str, str]]:
    """Query PostgreSQL columns grouped by (schema, table) using an open cursor."""
    cur.execute(DB_TABLES_SQL)
    tables: dict[tuple[str, str], dict[str, str]] = {}
    for schema_name, table_name, column_name, udt_name in cur.fetchall():
        tables.setdefault((str(schema_name), str(table_name)), {})[str(column_name)] = str(udt_name)
    return tables


def fetch_db_enum_tables(cur: Any) -> dict[tuple[str, str], frozenset[tuple[str, str]]]:
    """Query PostgreSQL regular/partitioned tables referencing each enum directly or indirectly."""
    cur.execute(DB_ENUM_TABLES_SQL)
    mapping: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for enum_schema, enum_name, table_schema, table_name in cur.fetchall():
        mapping.setdefault((str(enum_schema), str(enum_name)), set()).add(
            (str(table_schema), str(table_name))
        )
    return {k: frozenset(v) for k, v in mapping.items()}


def run_preconditions(cur: Any, queries: Sequence[str]) -> tuple[bool, list[str]]:
    """Execute read-only boolean/non-empty SQL preconditions on the open cursor."""
    failures: list[str] = []
    for raw in queries:
        stmt = validate_readonly_sql(raw)
        cur.execute(stmt)
        rows = cur.fetchall()
        if not rows:
            failures.append(f"precondition returned 0 rows: {stmt}")
            continue
        if len(rows) == 1 and len(rows[0]) == 1 and isinstance(rows[0][0], bool) and not rows[0][0]:
            failures.append(f"precondition evaluated false: {stmt}")
    return (not failures, failures)


def check_alembic_pending(
    conn: Any,
    *,
    alembic_config_path: str | None = None,
    alembic_script_location: str | None = None,
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    """Compare live Alembic revision heads on conn against script heads."""
    if not alembic_config_path and not alembic_script_location:
        return (False, (), ())
    alembic_cfg_mod = require_optional("alembic.config", extra="postgres")
    alembic_mig_mod = require_optional("alembic.runtime.migration", extra="postgres")
    alembic_script_mod = require_optional("alembic.script", extra="postgres")

    if alembic_config_path:
        cfg = alembic_cfg_mod.Config(alembic_config_path)
        if alembic_script_location:
            cfg.set_main_option("script_location", alembic_script_location)
        script = alembic_script_mod.ScriptDirectory.from_config(cfg)
    else:
        script = alembic_script_mod.ScriptDirectory(alembic_script_location)

    expected_heads = tuple(sorted(script.get_heads()))
    ctx = alembic_mig_mod.MigrationContext.configure(conn)
    current_heads = tuple(sorted(ctx.get_current_heads()))
    return (expected_heads != current_heads, expected_heads, current_heads)


def extract_columns_from_metadata(
    metadata: Any, *, default_schema: str = "public"
) -> dict[tuple[str, str], dict[str, str]]:
    """Extract {(schema, table): {column: type_name}} from SQLAlchemy MetaData."""
    if metadata is None or not hasattr(metadata, "tables"):
        return {}
    expected: dict[tuple[str, str], dict[str, str]] = {}
    for table_key, table in metadata.tables.items():
        schema_name = str(getattr(table, "schema", None) or default_schema)
        table_name = str(getattr(table, "name", None) or str(table_key).rsplit(".", 1)[-1])
        cols: dict[str, str] = {}
        for col in table.columns:
            cols[str(col.name)] = str(getattr(col.type, "name", None) or type(col.type).__name__)
        expected[(schema_name, table_name)] = cols
    return expected


def normalize_expected_tables(
    expected_tables: Mapping[tuple[str, str], Mapping[str, str]] | None,
    metadata: Any = None,
    *,
    default_schema: str = "public",
) -> dict[tuple[str, str], dict[str, str]]:
    """Combine explicit table expectations and SQLAlchemy MetaData tables cleanly."""
    out = extract_columns_from_metadata(metadata, default_schema=default_schema)
    if expected_tables:
        for table_key, cols in expected_tables.items():
            schema_name, table_name = str(table_key[0]), str(table_key[1])
            out[(schema_name, table_name)] = {str(k): str(v) for k, v in cols.items()}
    return out
