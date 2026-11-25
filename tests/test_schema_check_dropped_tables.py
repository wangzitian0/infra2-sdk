"""The pre-deploy schema gate tolerates a DB-only enum type that only dropped tables use (#3227).

Evidence: infra2 staging v3.1.176 blocked finance_report v0.2.99 (finance_report #3227).
Migration 0081 drops the tables `llm_providers` and `llm_scene_bindings` and the enum types
`llm_protocol_family_enum`, `llm_scene_enum` and `llm_reasoning_effort_enum`. The gate runs
before `alembic upgrade head`, so the database still has the three types and the code does
not. This is the contract-step mirror of the #1658 expand-step rule.

The rule reads only facts: the code's table names and the live database's type-to-table map.

### Mutation evidence

Each mutation was applied to `src/infra2_sdk/runtime/schema_check.py`, the named test failed,
and the mutation was then removed.

- M1: remove `if not migrations_pending: return ...` from `cover_with_dropped_tables`.
  `test_no_pending_migration_covers_nothing` fails.
- M2: remove `discrepancy.code_values == ()`.
  `test_a_type_that_the_code_declares_with_labels_blocks` fails.
- M3: remove `tables and` (the non-empty using-table set).
  `test_a_type_that_no_database_table_uses_blocks` fails.
- M4: remove `not tables & declared`.
  `test_a_type_that_a_code_table_still_uses_blocks` and
  `test_a_type_with_one_of_two_using_tables_still_in_code_blocks` fail.
- M5: compute `rollback_class` from the blocking list after the coverage.
  `test_main_passes_the_0081_shape_and_prints_class_c` fails (it prints class A).
- M6: remove the `try`/`except` around the dropped-table coverage in `main`.
  `test_main_blocks_when_the_enum_table_map_cannot_be_read` fails (the error escapes).
- M7: in `fetch_db_enum_tables`, keep only the last table of each type.
  `test_fetch_db_enum_tables_groups_the_tables_of_each_type` fails.
"""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from infra2_sdk.runtime import schema_check as gate
from infra2_sdk.runtime.schema_check import (
    EnumDiscrepancy,
    check_enums,
    cover_with_dropped_tables,
    table_names_from_metadata,
)

PROTOCOL = "llm_protocol_family_enum"
SCENE = "llm_scene_enum"
EFFORT = "llm_reasoning_effort_enum"
KEPT = "statement_run_kind_enum"

DB_ENUMS_0081 = {
    PROTOCOL: ["openai", "anthropic"],
    SCENE: ["extract", "classify"],
    EFFORT: ["low", "high"],
    KEPT: ["extract"],
}
ENUM_DB_TABLES_0081 = {
    PROTOCOL: frozenset({"llm_providers"}),
    SCENE: frozenset({"llm_scene_bindings"}),
    EFFORT: frozenset({"llm_scene_bindings"}),
    KEPT: frozenset({"statement_runs"}),
}
CODE_TABLES_0081 = frozenset({"users", "statement_runs"})


def _db_only(name: str, *labels: str) -> EnumDiscrepancy:
    return EnumDiscrepancy(name, (), labels, (), labels, ())


def _cover(discrepancies, enum_db_tables, code_tables, *, pending=True):
    return cover_with_dropped_tables(
        discrepancies,
        enum_db_tables=enum_db_tables,
        code_tables=code_tables,
        migrations_pending=pending,
    )


def test_the_0081_shape_is_covered() -> None:
    discrepancies = check_enums({KEPT: ("extract",)}, DB_ENUMS_0081)
    assert {d.enum_name for d in discrepancies} == {PROTOCOL, SCENE, EFFORT}
    blocking, covered = _cover(discrepancies, ENUM_DB_TABLES_0081, CODE_TABLES_0081)
    assert blocking == []
    assert sorted(covered) == [
        (PROTOCOL, ("llm_providers",)),
        (EFFORT, ("llm_scene_bindings",)),
        (SCENE, ("llm_scene_bindings",)),
    ]


def test_a_type_that_two_dropped_tables_use_is_covered() -> None:
    blocking, covered = _cover(
        [_db_only(SCENE, "extract")],
        {SCENE: frozenset({"llm_scene_bindings", "llm_providers"})},
        CODE_TABLES_0081,
    )
    assert blocking == []
    assert covered == [(SCENE, ("llm_providers", "llm_scene_bindings"))]


def test_no_pending_migration_covers_nothing() -> None:
    """The database is at the image's head and still has the type: drift, not a drop."""
    discrepancy = _db_only(PROTOCOL, "openai")
    assert _cover(
        [discrepancy], {PROTOCOL: frozenset({"llm_providers"})}, CODE_TABLES_0081, pending=False
    ) == ([discrepancy], [])


def test_a_type_that_a_code_table_still_uses_blocks() -> None:
    """The code changed the column of `llm_providers` to another type, but keeps the table."""
    discrepancy = _db_only(PROTOCOL, "openai")
    code_tables = CODE_TABLES_0081 | {"llm_providers"}
    assert _cover([discrepancy], {PROTOCOL: frozenset({"llm_providers"})}, code_tables) == (
        [discrepancy],
        [],
    )


def test_a_type_with_one_of_two_using_tables_still_in_code_blocks() -> None:
    discrepancy = _db_only(SCENE, "extract")
    code_tables = CODE_TABLES_0081 | {"llm_scene_bindings"}
    tables = {SCENE: frozenset({"llm_scene_bindings", "llm_providers"})}
    assert _cover([discrepancy], tables, code_tables) == ([discrepancy], [])


def test_a_type_that_the_code_declares_with_labels_blocks() -> None:
    """A partial label difference: the code still declares the type, so nothing drops it."""
    discrepancy = EnumDiscrepancy(PROTOCOL, ("openai",), ("openai", "gone"), (), ("gone",), ())
    tables = {PROTOCOL: frozenset({"llm_providers"})}
    assert _cover([discrepancy], tables, CODE_TABLES_0081) == ([discrepancy], [])


def test_a_casing_drift_blocks() -> None:
    discrepancy = EnumDiscrepancy(
        PROTOCOL, ("OPENAI",), ("openai",), ("OPENAI",), ("openai",), (("OPENAI", "openai"),)
    )
    tables = {PROTOCOL: frozenset({"llm_providers"})}
    assert _cover([discrepancy], tables, CODE_TABLES_0081) == ([discrepancy], [])


def test_a_type_that_no_database_table_uses_blocks() -> None:
    """An orphan type: no table proves that a pending migration drops it."""
    discrepancy = _db_only(PROTOCOL, "openai")
    assert _cover([discrepancy], {}, CODE_TABLES_0081) == ([discrepancy], [])


def test_covered_and_blocking_discrepancies_are_split_in_one_call() -> None:
    dropped, orphan = _db_only(PROTOCOL, "openai"), _db_only("orphan_enum", "a")
    tables = {PROTOCOL: frozenset({"llm_providers"})}
    assert _cover([dropped, orphan], tables, CODE_TABLES_0081) == (
        [orphan],
        [(PROTOCOL, ("llm_providers",))],
    )


def test_table_names_drop_the_schema_prefix() -> None:
    metadata = SimpleNamespace(tables={"public.users": object(), "statement_runs": object()})
    assert table_names_from_metadata(metadata) == frozenset({"users", "statement_runs"})


def test_table_names_reject_an_object_that_is_not_metadata() -> None:
    with pytest.raises(gate.GateNotEvaluated):
        table_names_from_metadata(SimpleNamespace())


def test_the_enum_table_query_is_one_read_only_select() -> None:
    sql = gate.DB_ENUM_TABLES_SQL
    assert gate._READ_ONLY_SQL_RE.match(sql)
    assert gate._FORBIDDEN_SQL_RE.search(sql) is None
    assert sql.count(";") == 1 and sql.endswith(";")


class _Connection:
    def __init__(self, rows):
        self._rows = rows
        self.queries: list[str] = []

    def cursor(self):
        return self

    def execute(self, query):
        self.queries.append(query)

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def test_fetch_db_enum_tables_groups_the_tables_of_each_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _Connection(
        [(SCENE, "llm_scene_bindings"), (SCENE, "llm_providers"), (PROTOCOL, "llm_providers")]
    )
    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda _url: connection))
    assert gate.fetch_db_enum_tables("postgresql://db/app") == {
        SCENE: frozenset({"llm_scene_bindings", "llm_providers"}),
        PROTOCOL: frozenset({"llm_providers"}),
    }
    assert connection.queries == [gate.DB_ENUM_TABLES_SQL]


def _column(name: str, labels: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(type=SimpleNamespace(name=name, enums=list(labels), native_enum=True))


@pytest.fixture
def run_app(monkeypatch: pytest.MonkeyPatch) -> str:
    """The code after 0081: `statement_runs` only, with the kept enum type."""
    module = ModuleType("schema_gate_drop_app")
    tables = {
        "statement_runs": SimpleNamespace(columns=[_column(KEPT, ("extract",))]),
        "users": SimpleNamespace(columns=[SimpleNamespace(type=object())]),
    }
    module.Base = SimpleNamespace(metadata=SimpleNamespace(tables=tables))
    monkeypatch.setitem(sys.modules, "schema_gate_drop_app", module)
    monkeypatch.setattr(gate, "fetch_db_enums", lambda _url: dict(DB_ENUMS_0081))
    db_tables = frozenset({"users", "statement_runs", "llm_providers", "llm_scene_bindings"})
    monkeypatch.setattr(gate, "fetch_db_tables", lambda _url: db_tables)
    monkeypatch.setattr(gate, "fetch_db_enum_tables", lambda _url: dict(ENUM_DB_TABLES_0081))
    monkeypatch.setattr(gate, "migrations_pending", lambda *_args: True)
    return "schema_gate_drop_app:Base.metadata"


def _run_main(metadata: str) -> int:
    return gate.main(["--metadata", metadata, "--db-url", "postgresql://db/app"])


def test_main_passes_the_0081_shape_and_prints_class_c(
    run_app: str, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = _run_main(run_app)
    out = capsys.readouterr().out
    assert rc == gate.EXIT_OK
    assert f"DROPPED_TABLE_COVERS: {PROTOCOL} (llm_providers)" in out
    assert f"DROPPED_TABLE_COVERS: {SCENE} (llm_scene_bindings)" in out
    assert f"DROPPED_TABLE_COVERS: {EFFORT} (llm_scene_bindings)" in out
    assert "3 covered by dropped tables" in out
    assert "ROLLBACK_CLASS: C" in out.splitlines()


def test_main_blocks_when_no_migration_is_pending(
    run_app: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(gate, "migrations_pending", lambda *_args: False)
    assert _run_main(run_app) == gate.EXIT_BLOCKED
    captured = capsys.readouterr()
    assert "DROPPED_TABLE_COVERS" not in captured.out
    assert PROTOCOL in captured.err
    assert "ROLLBACK_CLASS: C" in captured.out.splitlines()


def test_main_blocks_when_a_code_table_still_uses_the_type(
    run_app: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tables = sys.modules["schema_gate_drop_app"].Base.metadata.tables
    tables["llm_providers"] = SimpleNamespace(columns=[SimpleNamespace(type=object())])
    assert _run_main(run_app) == gate.EXIT_BLOCKED
    captured = capsys.readouterr()
    assert f"{PROTOCOL}: missing_in_db=()" in captured.err
    assert SCENE not in captured.err


def test_main_blocks_when_the_enum_table_map_cannot_be_read(
    run_app: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unreadable(_url):
        raise OSError("pg_attribute is unreadable")

    monkeypatch.setattr(gate, "fetch_db_enum_tables", unreadable)
    assert _run_main(run_app) == gate.EXIT_BLOCKED
    err = capsys.readouterr().err
    assert "NOTE: dropped-table coverage not applied (OSError)" in err
    assert PROTOCOL in err


def test_main_blocks_when_the_pending_check_fails(
    run_app: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(*_args):
        raise ValueError("alembic.ini has no script_location")

    monkeypatch.setattr(gate, "migrations_pending", broken)
    assert _run_main(run_app) == gate.EXIT_BLOCKED
    assert "NOTE: dropped-table coverage not applied (ValueError)" in capsys.readouterr().err
