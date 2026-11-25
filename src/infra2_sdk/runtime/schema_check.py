"""Pre-deploy schema and enum consistency check (moved from infra2, #239 Slice 2).

The check compares the native enum types of an application's SQLAlchemy ``MetaData`` with
the ``pg_enum`` labels of the target database before a deploy replaces containers
(infra2 #698: an enum case mismatch took finance_report down for 13 minutes).

- Bidirectional: ``missing_in_db`` and ``missing_in_code`` both block. Two exceptions apply
  only while migrations are pending:

  - Expand step (infra2 #1658): a whole enum type that the database lacks, used only by
    tables that the database lacks, does not block. See ``cover_with_new_tables``.
  - Contract step (finance_report #3227): a whole enum type that the code does not declare,
    used (directly or through a derived type) only by database tables that the code does not
    declare, does not block. A pending
    migration drops those tables and the type. See ``cover_with_dropped_tables``. The run
    still prints ``ROLLBACK_CLASS: C``, because a drop is not automatically reversible.
- Fail closed: a query failure or a parse error blocks.
- Never a silent pass: a check that cannot load its code side, or has no database, exits
  ``EXIT_NOT_EVALUATED`` (3), which blocks like a finding.

Exit codes: 0 verified, 1 blocked, 3 not evaluated.

The code side is the application's own metadata, so the check runs where the application
imports (its image or virtualenv)::

    python -m infra2_sdk.runtime.schema_check --metadata src.database:Base.metadata \
        --import src.orm_registry --app-path apps/backend --db-url "$DATABASE_URL"

The application declares its source in ``EnumSchemaSpec`` (or the CLI options); the SDK
keeps no table of applications. SQLAlchemy is duck-typed, so the SDK does not depend on it.
A libpq driver (``psycopg`` from the ``postgres`` extra, or ``psycopg2``) opens the
connection; ``alembic`` is needed only for the new-table and dropped-table coverage.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import re
import sys
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from infra2_sdk.runtime.postgres import (
    PostgresSettings,
    _normalize_dsn,
    redact_postgres_error,
)

EXIT_OK = 0
EXIT_BLOCKED = 1
# 2 is argparse's usage error; "not evaluated" gets its own code so a caller can tell
# "the schemas differ" from "nothing was compared" — both still block.
EXIT_NOT_EVALUATED = 3


class GateNotEvaluated(Exception):
    """An input the gate needs is unavailable, so there is no verdict — which blocks."""


@dataclass(frozen=True)
class EnumSchemaSpec:
    """Where an application's code-side enum truth and data preconditions live.

    ``metadata`` names a SQLAlchemy ``MetaData`` as ``module:attribute.path``; every
    native enum column type registered on it is one Postgres enum type (its ``name``)
    with its labels (``enums``). ``imports`` are imported first, for their side effect
    of registering every mapped class on that metadata. ``app_path`` says which
    directory of the service's checkout has to be on ``sys.path``.
    ``requires_migration`` sets an optional minimum migration version floor checked
    against ``migration_table`` (default ``alembic_version.version_num``) (#609).
    ``preconditions`` declares named read-only SQL predicates ``(name, sql, remedy)``
    that must all hold before any container is replaced (#609).
    """

    metadata: str
    imports: tuple[str, ...] = ()
    app_path: str = ""
    requires_migration: str | None = None
    migration_table: str = "alembic_version"
    migration_column: str = "version_num"
    preconditions: tuple[tuple[str, str, str], ...] = ()


@dataclass(frozen=True)
class EnumDiscrepancy:
    enum_name: str
    code_values: tuple[str, ...]
    db_values: tuple[str, ...]
    missing_in_db: tuple[str, ...]
    missing_in_code: tuple[str, ...]
    casing_mismatches: tuple[tuple[str, str], ...]

    @property
    def has_error(self) -> bool:
        """Bidirectional: any direction of mismatch is an error.

        - missing_in_db: code has values DB doesn't → deploy will INSERT unknown enum → crash
        - missing_in_code: DB has values code doesn't know → runtime deserialization failures
        - casing_mismatches: same logical value, different case → silent data corruption
        """
        return bool(self.missing_in_db or self.missing_in_code or self.casing_mismatches)


# Infra-022 T3.2 / TODOWRITE:20: the safety class an automatic-rollback circuit breaker
# would need before this deploy's schema state could be reversed. Only Class A may ever
# auto-rollback; Class C never may (DROP/RENAME/tightened constraints require a human
# forward-fix, production_resilience_and_dr.md T3.2). This module has no visibility into
# raw migration DDL — it can only classify from the SAME bidirectional enum comparison
# the gate already computes, so it distinguishes exactly two tiers, not three: A
# (nothing destructive observed) and C (something was).
#
# Both ``missing_in_code`` and ``casing_mismatches`` are treated as Class C — this is a
# DELIBERATE choice below (``or``), not a derived necessity: they are NOT always
# coupled. ``compare_enum_values`` computes them from independent comparisons (a plain
# set difference for missing_in_code; a separate case-insensitive scan for
# casing_mismatches), so casing drift CAN occur with an empty ``missing_in_code`` — e.g.
# code declaring both case variants (``["PENDING", "pending"]``) against a DB with only
# ``["pending"]`` sets ``casing_mismatches=(("PENDING","pending"),)`` while
# ``missing_in_code=()`` (the lowercase member alone already satisfies the DB side).
# Class C classifies it anyway because same-value casing drift is "silent data
# corruption" per ``EnumDiscrepancy.has_error`` — never treated as the safe tier.
ROLLBACK_CLASS_A = "A"  # no destructive signal observed -- automatic rollback permitted
ROLLBACK_CLASS_C = "C"  # DB carries structure the code no longer declares -- never auto


def classify_rollback(discrepancies: Sequence[EnumDiscrepancy]) -> str:
    """ROLLBACK_CLASS for this comparison (Infra-022 T3.2's rollback safety class).

    - Class C: any discrepancy has ``missing_in_code`` (the DB carries an enum label, or
      a whole enum type, the code no longer declares) OR a same-value casing drift —
      independent signals, both treated as Class C (see the module-level comment above
      for why they are not always coupled). ``missing_in_code`` is the direction that
      corresponds to a DROP/RENAME having already reached the database; the old code
      cannot be safely restored against it. This also covers a DB-only enum type
      (``code_values=()``).
    - Class A: zero discrepancies, or only ``missing_in_db`` (code is ahead of a
      not-yet-migrated DB — purely additive; old code neither reads nor needs the new
      label).
    """
    if any(disc.missing_in_code or disc.casing_mismatches for disc in discrepancies):
        return ROLLBACK_CLASS_C
    return ROLLBACK_CLASS_A


def compare_enum_values(
    enum_name: str, code_values: Sequence[str], db_values: Sequence[str]
) -> EnumDiscrepancy:
    """Compare code enum values against database enum values for exact match and case divergence."""
    code_set = set(code_values)
    db_set = set(db_values)

    missing_in_db = tuple(sorted(code_set - db_set))
    missing_in_code = tuple(sorted(db_set - code_set))

    # Detect casing divergence: e.g. code has 'PENDING' while DB has 'pending'
    casing_mismatches: list[tuple[str, str]] = []
    db_lower_map = {v.lower(): v for v in db_values}
    for cv in code_values:
        if cv not in db_set and cv.lower() in db_lower_map:
            casing_mismatches.append((cv, db_lower_map[cv.lower()]))

    return EnumDiscrepancy(
        enum_name=enum_name,
        code_values=tuple(code_values),
        db_values=tuple(db_values),
        missing_in_db=missing_in_db,
        missing_in_code=missing_in_code,
        casing_mismatches=tuple(casing_mismatches),
    )


def extract_enum_values(enum_cls: type[Enum]) -> list[str]:
    """Extract string values from a Python Enum class."""
    values = []
    for member in enum_cls:
        if isinstance(member.value, str):
            values.append(member.value)
        else:
            values.append(str(member.value))
    return values


def query_db_enums(connection_or_cursor: Any) -> dict[str, list[str]]:
    """Query PostgreSQL pg_type and pg_enum to get all defined database enums and their labels."""
    query = """
    SELECT
        t.typname AS enum_name,
        e.enumlabel AS enum_value
    FROM pg_type t
    JOIN pg_enum e ON t.oid = e.enumtypid
    JOIN pg_catalog.pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
    ORDER BY t.typname, e.enumsortorder;
    """
    if hasattr(connection_or_cursor, "cursor"):
        cursor = connection_or_cursor.cursor()
        cursor.execute(query)
        rows = cursor.fetchall()
    elif hasattr(connection_or_cursor, "execute"):
        connection_or_cursor.execute(query)
        rows = connection_or_cursor.fetchall()
    elif hasattr(connection_or_cursor, "fetchall"):
        rows = connection_or_cursor.fetchall()
    else:
        # An object that can neither run the query nor hand back rows must not read as
        # "the database has no enums".
        raise TypeError(f"cannot query enums through {type(connection_or_cursor).__name__}")

    enums: dict[str, list[str]] = {}
    for row in rows:
        name = row[0] if isinstance(row, (tuple, list)) else row.get("enum_name")
        val = row[1] if isinstance(row, (tuple, list)) else row.get("enum_value")
        enums.setdefault(name, []).append(val)
    return enums


def check_enums(
    code_enums: dict[str, type[Enum] | Sequence[str]],
    db_enums: dict[str, Sequence[str]],
) -> list[EnumDiscrepancy]:
    """Check a mapping of code enums against database enums.

    Bidirectional: checks both directions (code→DB and DB→code).
    Also detects DB enums that exist but have no code counterpart.
    """
    discrepancies: list[EnumDiscrepancy] = []
    for name, code_def in code_enums.items():
        if isinstance(code_def, type) and issubclass(code_def, Enum):
            code_vals = extract_enum_values(code_def)
        else:
            code_vals = list(code_def)

        db_vals = db_enums.get(name, [])
        if not db_vals:
            # DB enum missing entirely
            discrepancies.append(
                EnumDiscrepancy(
                    enum_name=name,
                    code_values=tuple(code_vals),
                    db_values=(),
                    missing_in_db=tuple(code_vals),
                    missing_in_code=(),
                    casing_mismatches=(),
                )
            )
            continue

        disc = compare_enum_values(name, code_vals, db_vals)
        if disc.has_error:
            discrepancies.append(disc)

    # Reverse check: DB enums not declared in code at all
    for db_name in db_enums:
        if db_name not in code_enums:
            discrepancies.append(
                EnumDiscrepancy(
                    enum_name=db_name,
                    code_values=(),
                    db_values=tuple(db_enums[db_name]),
                    missing_in_db=(),
                    missing_in_code=tuple(db_enums[db_name]),
                    casing_mismatches=(),
                )
            )

    return discrepancies


def cover_with_new_tables(
    discrepancies: Sequence[EnumDiscrepancy],
    *,
    enum_tables: Mapping[str, frozenset[str]],
    db_tables: Collection[str],
    migrations_pending: bool,
) -> tuple[list[EnumDiscrepancy], list[tuple[str, tuple[str, ...]]]]:
    """Split off a whole enum type that only tables missing from the database use (#1658).

    The image runs ``alembic upgrade head`` after this gate. When migrations are pending
    (``migrations_pending``), a table that the database lacks is created by one of them, and
    a column of that table can only name a type that exists by then, so the same migration
    creates the type. That holds if the migration declares the column with the ORM's type;
    the service's CI proves it, labels included (finance_report
    ``scripts/check_migrated_enums.py``, job ``schema-migrations``). Nothing here reads
    migration code. Covered: a type that the database lacks entirely, when every table that
    uses it is missing from the database. Always blocking: a label that an existing type
    lacks, a DB-only label, a casing drift, a type that any table already in the database
    uses, and every type when no migration is pending (a database that is already at the
    image's head and still lacks the table is drift). Returns ``(blocking, covered)`` with
    ``covered`` as ``(enum name, its new tables)``.
    """
    if not migrations_pending:
        return list(discrepancies), []
    existing = set(db_tables)
    blocking: list[EnumDiscrepancy] = []
    covered: list[tuple[str, tuple[str, ...]]] = []
    for discrepancy in discrepancies:
        tables = enum_tables.get(discrepancy.enum_name, frozenset())
        if not discrepancy.db_values and tables and not tables & existing:
            covered.append((discrepancy.enum_name, tuple(sorted(tables))))
        else:
            blocking.append(discrepancy)
    return blocking, covered


def cover_with_dropped_tables(
    discrepancies: Sequence[EnumDiscrepancy],
    *,
    enum_db_tables: Mapping[str, frozenset[str]],
    code_enums: Collection[str],
    code_tables: Collection[str],
    migrations_pending: bool,
) -> tuple[list[EnumDiscrepancy], list[tuple[str, tuple[str, ...]]]]:
    """Split off a whole DB-only enum type that only tables the code dropped use (#3227).

    This is the contract-step mirror of ``cover_with_new_tables``. The image runs
    ``alembic upgrade head`` after this gate. A pending migration that drops tables and
    their enum types leaves the database ahead of the code until it runs. Nothing here
    reads migration code. Covered: a type whose name is not in ``code_enums`` (the code
    declares no type of that name), when ``enum_db_tables`` names at least one database
    table that uses the type and no such table is in ``code_tables``. Always blocking: no
    pending migration, a type that the code declares (a label difference, a casing drift,
    or zero labels), a type that no database table uses, and a type that a table of the
    code still uses. Returns ``(blocking, covered)``; ``covered`` holds ``(enum name, tables)``.
    """
    if not migrations_pending:
        return list(discrepancies), []
    declared = set(code_tables)
    declared_enums = set(code_enums)
    blocking: list[EnumDiscrepancy] = []
    covered: list[tuple[str, tuple[str, ...]]] = []
    for discrepancy in discrepancies:
        tables = enum_db_tables.get(discrepancy.enum_name, frozenset())
        if discrepancy.enum_name not in declared_enums and tables and not tables & declared:
            covered.append((discrepancy.enum_name, tuple(sorted(tables))))
        else:
            blocking.append(discrepancy)
    return blocking, covered


def _resolve(reference: str) -> Any:
    module_name, _, attribute_path = reference.partition(":")
    target: Any = importlib.import_module(module_name)
    for part in filter(None, attribute_path.split(".")):
        target = getattr(target, part)
    return target


def _enum_type(column_type: Any) -> Any | None:
    """The native enum type behind a column type, or None.

    Duck-typed on what SQLAlchemy's ``Enum`` exposes (``name``, ``enums``,
    ``native_enum``) so infra2 does not need SQLAlchemy installed; an ``ARRAY`` of an
    enum and a ``TypeDecorator`` over one are unwrapped.
    """
    for candidate in (
        column_type,
        getattr(column_type, "item_type", None),
        getattr(column_type, "impl_instance", None),
    ):
        if candidate is None:
            continue
        labels = getattr(candidate, "enums", None)
        if (
            isinstance(labels, (list, tuple))
            and getattr(candidate, "name", None)
            and getattr(candidate, "native_enum", True)
        ):
            return candidate
    return None


def _native_enum_columns(metadata: Any) -> Iterator[tuple[str, Any]]:
    """``(table name, native enum type)`` for every native enum column on ``metadata``."""
    tables = getattr(metadata, "tables", None)
    if not isinstance(tables, Mapping):
        raise GateNotEvaluated(
            f"{type(metadata).__name__} is not a SQLAlchemy MetaData (no .tables)"
        )
    for key, table in tables.items():
        for column in table.columns:
            enum_type = _enum_type(column.type)
            if enum_type is not None:
                yield key.rsplit(".", 1)[-1], enum_type


def enums_from_metadata(metadata: Any) -> dict[str, tuple[str, ...]]:
    """``{postgres enum type name: labels}`` for every native enum column on ``metadata``."""
    enums: dict[str, tuple[str, ...]] = {}
    for _table, enum_type in _native_enum_columns(metadata):
        labels = tuple(str(label) for label in enum_type.enums)
        known = enums.setdefault(enum_type.name, labels)
        if set(known) != set(labels):
            raise GateNotEvaluated(
                f"enum type {enum_type.name!r} is declared with different labels "
                f"on two columns: {sorted(known)} vs {sorted(labels)}"
            )
    return enums


def enum_tables_from_metadata(metadata: Any) -> dict[str, frozenset[str]]:
    """``{postgres enum type name: names of the tables whose columns use it}`` (#1658)."""
    users: dict[str, set[str]] = {}
    for table, enum_type in _native_enum_columns(metadata):
        users.setdefault(enum_type.name, set()).add(table)
    return {name: frozenset(tables) for name, tables in users.items()}


def table_names_from_metadata(metadata: Any) -> frozenset[str]:
    """The names of every table on ``metadata``, without a schema prefix (#3227)."""
    tables = getattr(metadata, "tables", None)
    if not isinstance(tables, Mapping):
        raise GateNotEvaluated(
            f"{type(metadata).__name__} is not a SQLAlchemy MetaData (no .tables)"
        )
    return frozenset(str(key).rsplit(".", 1)[-1] for key in tables)


def load_code_enums(
    spec: EnumSchemaSpec, *, app_path: str | None = None
) -> dict[str, tuple[str, ...]]:
    """The application's code-side enums; raises ``GateNotEvaluated`` when they can't be read.

    A module that does not import, or a metadata object with no native enum types, means
    nothing can be compared: never "0 discrepancies".
    """
    if app_path:
        directory = Path(app_path).expanduser().resolve()
        if not directory.is_dir():
            raise GateNotEvaluated(f"--app-path {app_path} is not a directory")
        if str(directory) not in sys.path:
            sys.path.insert(0, str(directory))
    try:
        for module_name in spec.imports:
            importlib.import_module(module_name)
        metadata = _resolve(spec.metadata)
    except Exception as exc:  # noqa: BLE001 - any import-time failure means "not loaded"
        raise GateNotEvaluated(
            f"cannot load enums from {spec.metadata}: {type(exc).__name__}: {exc} "
            "(run it in the application's own Python environment)"
        ) from exc
    enums = enums_from_metadata(metadata)
    if not enums:
        raise GateNotEvaluated(
            f"{spec.metadata} declares no native enum types; an empty code side cannot be compared"
        )
    return enums


def libpq_url(db_url: str) -> str:
    """Drop a SQLAlchemy driver suffix (``postgresql+asyncpg://``) libpq rejects."""
    return _normalize_dsn(db_url)


_SAFE_SQL_IDENTIFIER_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_.]*\Z")
_READ_ONLY_SQL_RE = re.compile(r"\A\s*SELECT\b", re.IGNORECASE)
_FORBIDDEN_SQL_RE = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|TRUNCATE|CREATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def query_applied_migrations(
    connection_or_cursor: Any,
    *,
    migration_table: str = "alembic_version",
    migration_column: str = "version_num",
) -> tuple[str, ...]:
    """Query the applied migration versions recorded in the target database (#609)."""
    if not _SAFE_SQL_IDENTIFIER_RE.match(migration_table) or not _SAFE_SQL_IDENTIFIER_RE.match(
        migration_column
    ):
        raise ValueError(
            f"invalid migration table/column identifier: {migration_table}.{migration_column}"
        )
    cursor = (
        connection_or_cursor.cursor()
        if hasattr(connection_or_cursor, "cursor")
        else connection_or_cursor
    )
    cursor.execute(f"SELECT {migration_column} FROM {migration_table} ORDER BY {migration_column};")
    rows = cursor.fetchall()
    versions: list[str] = []
    for row in rows or ():
        val = row[0] if isinstance(row, (tuple, list)) else row.get(migration_column)
        if val is not None and str(val).strip():
            versions.append(str(val).strip())
    return tuple(versions)


def check_data_preconditions(
    connection_or_cursor: Any,
    *,
    requires_migration: str | None = None,
    migration_table: str = "alembic_version",
    migration_column: str = "version_num",
    preconditions: Sequence[tuple[str, str, str]] = (),
) -> tuple[tuple[str, ...], list[str]]:
    """Verify migration floor and named read-only SQL data preconditions (#609).

    Returns ``(applied_migrations, failures)``. Fails closed when ``requires_migration``
    is unmet or any read-only SQL predicate returns false/0/None or errors.
    """
    applied: tuple[str, ...] = ()
    failures: list[str] = []
    cursor = (
        connection_or_cursor.cursor()
        if hasattr(connection_or_cursor, "cursor")
        else connection_or_cursor
    )
    if requires_migration is not None:
        try:
            applied = query_applied_migrations(
                cursor,
                migration_table=migration_table,
                migration_column=migration_column,
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"migration_floor ({requires_migration}): could not query "
                f"{migration_table}.{migration_column}: {exc}"
            )
        else:
            if not applied:
                failures.append(
                    f"migration_floor ({requires_migration}): {migration_table} is empty "
                    f"(required >= {requires_migration})"
                )
            elif max(applied) < str(requires_migration).strip():
                failures.append(
                    f"migration_floor ({requires_migration}): applied migration "
                    f"{max(applied)!r} is older than required floor {requires_migration!r}"
                )

    for name, sql_predicate, remedy in preconditions:
        if not _READ_ONLY_SQL_RE.match(sql_predicate or "") or _FORBIDDEN_SQL_RE.search(
            sql_predicate or ""
        ):
            failures.append(f"{name}: precondition query must be a read-only SELECT statement")
            continue
        try:
            cursor.execute(sql_predicate)
            rows = cursor.fetchall()
            scalar = (
                rows[0][0]
                if rows and isinstance(rows[0], (tuple, list)) and rows[0]
                else (
                    next(iter(rows[0].values()))
                    if rows and isinstance(rows[0], Mapping) and rows[0]
                    else None
                )
            )
            satisfied = bool(scalar) and str(scalar).strip().lower() not in (
                "false",
                "f",
                "0",
                "no",
                "none",
                "",
            )
            if not satisfied:
                remedy_suffix = f" — remediation: {remedy}" if remedy else ""
                failures.append(
                    f"{name}: predicate ({sql_predicate}) returned {scalar!r}{remedy_suffix}"
                )
        except Exception as exc:  # noqa: BLE001
            remedy_suffix = f" — remediation: {remedy}" if remedy else ""
            failures.append(f"{name}: query failed ({exc}){remedy_suffix}")

    return applied, failures


def _with_libpq_cursor(db_url: str, fn: Any) -> Any:
    """Open one libpq (psycopg/psycopg2) connection and pass a cursor to ``fn``."""
    url = libpq_url(db_url)
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        import psycopg2  # type: ignore[import-not-found]

        with contextlib.closing(psycopg2.connect(url)) as conn, conn, conn.cursor() as cur:
            return fn(cur)
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        return fn(cur)


def fetch_db_enums(db_url: str) -> dict[str, list[str]]:
    return _with_libpq_cursor(db_url, query_db_enums)


def evaluate_db_preconditions(
    db_url: str, source: EnumSchemaSpec
) -> tuple[tuple[str, ...], list[str]]:
    """Run declarative migration-floor and SQL data preconditions when configured (#609)."""
    if source.requires_migration is None and not source.preconditions:
        return (), []
    return _with_libpq_cursor(
        db_url,
        lambda cur: check_data_preconditions(
            cur,
            requires_migration=source.requires_migration,
            migration_table=source.migration_table,
            migration_column=source.migration_column,
            preconditions=source.preconditions,
        ),
    )


# Visible to the connecting role only (information_schema lists the tables that the role
# owns or has a privilege on); schema names are dropped, matching the ORM's table names.
ALEMBIC_INI = "alembic.ini"

DB_TABLES_SQL = (
    "SELECT table_name FROM information_schema.tables "
    "WHERE table_schema NOT IN ('pg_catalog', 'information_schema');"
)


def fetch_db_tables(db_url: str) -> frozenset[str]:
    """The names of every table in the target database (#1658)."""

    def read(cursor: Any) -> frozenset[str]:
        cursor.execute(DB_TABLES_SQL)
        return frozenset(str(row[0]) for row in cursor.fetchall())

    return _with_libpq_cursor(db_url, read)


# Each enum type with the tables whose columns use it (#3227). ``derived`` closes over every
# type that is built on the enum type: a domain (``typbasetype``), an array (``typarray``), a
# composite type or table row type with a field of the type (``reltype``), a range
# (``rngsubtype``) and a multirange (``rngmultitypid``, PostgreSQL 14 and later). A column of
# any derived type in an ordinary or partitioned table counts, and so does a table whose own
# row type is derived. Views, materialized views and foreign tables do not count. Schema names
# are dropped, matching ``query_db_enums`` and the ORM's table names. On a server that cannot
# run this query, the query fails and the coverage blocks.
DB_ENUM_TABLES_SQL = (
    "WITH RECURSIVE edges(base, derived) AS ("
    "SELECT typbasetype, oid FROM pg_catalog.pg_type WHERE typtype = 'd' "
    "UNION ALL SELECT oid, typarray FROM pg_catalog.pg_type WHERE typarray <> 0 "
    "UNION ALL SELECT a.atttypid, c.reltype FROM pg_catalog.pg_attribute a "
    "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
    "WHERE a.attnum > 0 AND NOT a.attisdropped AND c.reltype <> 0 "
    "UNION ALL SELECT rngsubtype, rngtypid FROM pg_catalog.pg_range "
    "UNION ALL SELECT rngtypid, rngmultitypid FROM pg_catalog.pg_range"
    "), derived(enum_oid, type_oid) AS ("
    "SELECT t.oid, t.oid FROM pg_catalog.pg_type t "
    "JOIN pg_catalog.pg_namespace tn ON tn.oid = t.typnamespace "
    "WHERE t.typtype = 'e' AND tn.nspname NOT IN ('pg_catalog', 'information_schema') "
    "UNION SELECT d.enum_oid, e.derived FROM derived d "
    "JOIN edges e ON e.base = d.type_oid"
    "), users(enum_oid, rel_oid) AS ("
    "SELECT d.enum_oid, a.attrelid FROM derived d "
    "JOIN pg_catalog.pg_attribute a ON a.atttypid = d.type_oid "
    "WHERE a.attnum > 0 AND NOT a.attisdropped "
    "UNION SELECT d.enum_oid, c.oid FROM derived d "
    "JOIN pg_catalog.pg_class c ON c.reltype = d.type_oid"
    ") "
    "SELECT DISTINCT t.typname, c.relname FROM users u "
    "JOIN pg_catalog.pg_type t ON t.oid = u.enum_oid "
    "JOIN pg_catalog.pg_class c ON c.oid = u.rel_oid AND c.relkind IN ('r', 'p') "
    "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
    "WHERE n.nspname NOT IN ('pg_catalog', 'information_schema');"
)


def fetch_db_enum_tables(db_url: str) -> dict[str, frozenset[str]]:
    """``{database enum type name: names of the database tables that use it}`` (#3227).

    The query is a read-only SELECT on the system catalog. A failure raises, and the caller
    treats a raise as "nothing covered".
    """

    def read(cursor: Any) -> dict[str, frozenset[str]]:
        cursor.execute(DB_ENUM_TABLES_SQL)
        users: dict[str, set[str]] = {}
        for row in cursor.fetchall():
            users.setdefault(str(row[0]), set()).add(str(row[1]))
        return {name: frozenset(tables) for name, tables in users.items()}

    return _with_libpq_cursor(db_url, read)


def migrations_pending(db_url: str, source: EnumSchemaSpec, app_path: str | None) -> bool:
    """True when the image has a migration that the database has not run (#1658).

    Uses alembic's own script directory, which the image carries. Every failure raises, and
    the caller treats a raise as "nothing covered": no ``script_location`` in alembic.ini, a
    missing alembic.ini, several version rows, a row that does not resolve to a revision
    (``base``, an unknown id, or a symbol the image does not know), several heads in the
    image. A database with no version row is not treated as pending: the gate blocks,
    although alembic would run every migration from base.
    """
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    base = Path(app_path).expanduser() if app_path else Path.cwd()
    config = Config(str(base / ALEMBIC_INI))
    script_location = config.get_main_option("script_location")
    if script_location is None:
        raise ValueError(f"{ALEMBIC_INI} has no script_location")
    location = Path(script_location)
    config.set_main_option(
        "script_location", str(location if location.is_absolute() else base / location)
    )
    script = ScriptDirectory.from_config(config)
    rows = _with_libpq_cursor(
        db_url,
        lambda cursor: query_applied_migrations(
            cursor,
            migration_table=source.migration_table,
            migration_column=source.migration_column,
        ),
    )
    if len(rows) > 1:
        raise ValueError(
            f"{source.migration_table} holds {len(rows)} rows; one head allows one row"
        )
    if not rows:
        return False
    applied = script.get_revision(rows[0])
    if applied is None:
        raise ValueError(f"database revision {rows[0]!r} resolves to no revision")
    return applied.revision != script.get_current_head()


def _spec_from_args(args: Any) -> EnumSchemaSpec:
    preconditions = []
    for raw in args.precondition or ():
        name, sep, rest = raw.partition("=")
        sql, _, remedy = rest.partition("::")
        if not sep or not name or not sql:
            raise ValueError(f"--precondition must be NAME=SELECT ...[::remedy], not {raw!r}")
        preconditions.append((name, sql, remedy))
    return EnumSchemaSpec(
        metadata=args.metadata,
        imports=tuple(args.imports or ()),
        app_path=args.app_path or "",
        requires_migration=args.requires_migration,
        preconditions=tuple(preconditions),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m infra2_sdk.runtime.schema_check",
        description="Pre-deploy schema and enum check (0 verified, 1 blocked, 3 not evaluated)",
    )
    parser.add_argument("--metadata", required=True, help="SQLAlchemy MetaData as module:attr.path")
    parser.add_argument(
        "--import", dest="imports", action="append", help="module to import first (repeatable)"
    )
    parser.add_argument("--app-path", default=None, help="directory put first on sys.path")
    parser.add_argument("--db-url", default="", help="Postgres URL of the target database")
    parser.add_argument("--requires-migration", default=None, help="minimum migration version")
    parser.add_argument(
        "--precondition", action="append", help="NAME=SELECT ...[::remedy] (repeatable)"
    )
    parser.add_argument("--label", default="", help="application name in the messages")
    args = parser.parse_args(argv)
    label = args.label or args.metadata

    reasons: list[str] = []
    if not args.db_url:
        reasons.append("no database URL (--db-url)")
    code_enums: dict[str, tuple[str, ...]] = {}
    try:
        spec = _spec_from_args(args)
        code_enums = load_code_enums(spec, app_path=args.app_path)
    except (GateNotEvaluated, ValueError) as exc:
        reasons.append(str(exc))
    if reasons:
        print(
            f"NOT EVALUATED — deploy blocked: pre-deploy schema check for {label}:", file=sys.stderr
        )
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return EXIT_NOT_EVALUATED

    db_url = args.db_url
    try:
        db_enums = fetch_db_enums(db_url)
        applied_migrations, precondition_failures = evaluate_db_preconditions(db_url, spec)
    except Exception as exc:
        detail = str(exc)
        with contextlib.suppress(ValueError):
            detail = redact_postgres_error(detail, PostgresSettings(dsn=db_url))
        print(
            f"ERROR: Failed to connect to DB for pre-deploy schema check: {detail}",
            file=sys.stderr,
        )
        return EXIT_BLOCKED

    if applied_migrations:
        print(f"APPLIED_MIGRATION: {','.join(applied_migrations)}")

    discrepancies = check_enums(code_enums, db_enums)
    # The class comes from every discrepancy, covered or not: a covered drop is still not
    # automatically reversible (Class C), and a covered new type is additive (Class A).
    rollback_class = classify_rollback(discrepancies)
    covered: list[tuple[str, tuple[str, ...]]] = []
    dropped: list[tuple[str, tuple[str, ...]]] = []
    pending = False
    if discrepancies:
        try:
            pending = migrations_pending(db_url, spec, args.app_path)
        except Exception as exc:  # noqa: BLE001 - fail closed: an unreadable input covers nothing
            print(f"NOTE: new-table coverage not applied ({type(exc).__name__})", file=sys.stderr)
            print(
                f"NOTE: dropped-table coverage not applied ({type(exc).__name__})",
                file=sys.stderr,
            )
    if pending:
        try:
            discrepancies, covered = cover_with_new_tables(
                discrepancies,
                enum_tables=enum_tables_from_metadata(_resolve(spec.metadata)),
                db_tables=fetch_db_tables(db_url),
                migrations_pending=pending,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed: an unreadable input covers nothing
            covered = []
            print(f"NOTE: new-table coverage not applied ({type(exc).__name__})", file=sys.stderr)
        try:
            discrepancies, dropped = cover_with_dropped_tables(
                discrepancies,
                enum_db_tables=fetch_db_enum_tables(db_url),
                code_enums=code_enums,
                code_tables=table_names_from_metadata(_resolve(spec.metadata)),
                migrations_pending=pending,
            )
        except Exception as exc:  # noqa: BLE001 - fail closed: an unreadable input covers nothing
            dropped = []
            print(
                f"NOTE: dropped-table coverage not applied ({type(exc).__name__})",
                file=sys.stderr,
            )
    for enum_name, tables in covered:
        print(f"NEW_TABLE_COVERS: {enum_name} ({', '.join(tables)})")
    for enum_name, tables in dropped:
        print(f"DROPPED_TABLE_COVERS: {enum_name} ({', '.join(tables)})")
    # Stable stdout line: infra2 common/deploy/schema_gate.py parses it in both branches.
    if discrepancies or precondition_failures:
        if discrepancies:
            print(f"ERROR: Schema discrepancies found for {label}:", file=sys.stderr)
            for disc in discrepancies:
                print(
                    f"  - {disc.enum_name}: missing_in_db={disc.missing_in_db}, "
                    f"missing_in_code={disc.missing_in_code}, casing={disc.casing_mismatches}",
                    file=sys.stderr,
                )
        if precondition_failures:
            print(f"ERROR: Data preconditions failed for {label}:", file=sys.stderr)
            for failure in precondition_failures:
                print(f"  - {failure}", file=sys.stderr)
        print(f"ROLLBACK_CLASS: {rollback_class}")
        return EXIT_BLOCKED

    print(
        f"Pre-deploy schema check verified for service: {label} "
        f"({len(code_enums)} enum types, 0 discrepancies"
        f"{f', {len(covered)} covered by new tables' if covered else ''}"
        f"{f', {len(dropped)} covered by dropped tables' if dropped else ''})"
    )
    print(f"ROLLBACK_CLASS: {rollback_class}")
    return EXIT_OK


__all__ = [
    "EXIT_BLOCKED",
    "EXIT_NOT_EVALUATED",
    "EXIT_OK",
    "EnumDiscrepancy",
    "EnumSchemaSpec",
    "GateNotEvaluated",
    "check_enums",
    "classify_rollback",
    "compare_enum_values",
    "cover_with_dropped_tables",
    "cover_with_new_tables",
    "enums_from_metadata",
    "load_code_enums",
    "main",
]


if __name__ == "__main__":
    sys.exit(main())
