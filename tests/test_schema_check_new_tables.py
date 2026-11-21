"""The pre-deploy schema gate tolerates an enum type that only new tables use (#1658).

Evidence: soak run 43360538840 of infra2 v3.1.58 blocked finance_report v0.2.65 because migration
0074 adds the table `statement_runs` with four enum columns. The gate compares the image's enum
types with the live database before the image's entrypoint runs `alembic upgrade head`, so the
types of a new table are always missing.

Two independent reviews of earlier versions showed that reading migration files (text, then ast)
cannot prove what `upgrade()` does. The rule here reads only facts: the ORM's table-to-type map
and the live database's table list.
"""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from infra2_sdk.runtime import schema_check as gate
from infra2_sdk.runtime.schema_check import (
    EnumDiscrepancy,
    cover_with_new_tables,
    enum_tables_from_metadata,
)

KIND = "statement_run_kind_enum"
STATE = "statement_run_state_enum"


def _missing(name: str, *labels: str) -> EnumDiscrepancy:
    return EnumDiscrepancy(name, labels, (), labels, (), ())


def _cover(discrepancies, enum_tables, db_tables, *, pending=True):
    return cover_with_new_tables(
        discrepancies,
        enum_tables=enum_tables,
        db_tables=db_tables,
        migrations_pending=pending,
    )


def test_a_type_that_only_a_new_table_uses_is_covered() -> None:
    blocking, covered = _cover(
        [_missing(KIND, "extract", "classify")],
        {KIND: frozenset({"statement_runs"})},
        {"users", "statements"},
    )
    assert blocking == []
    assert covered == [(KIND, ("statement_runs",))]


def test_a_type_that_only_two_new_tables_use_is_covered() -> None:
    blocking, covered = _cover(
        [_missing(KIND, "extract")],
        {KIND: frozenset({"statement_runs", "statement_run_log"})},
        {"users"},
    )
    assert blocking == []
    assert covered == [(KIND, ("statement_run_log", "statement_runs"))]


def test_a_type_that_a_new_table_and_an_existing_table_use_blocks() -> None:
    discrepancy = _missing(KIND, "extract")
    tables = {KIND: frozenset({"statement_runs", "statements"})}
    assert _cover([discrepancy], tables, {"statements"}) == ([discrepancy], [])


def test_a_type_that_is_missing_while_its_table_exists_blocks() -> None:
    """Drift: the table is in the database, so no pending migration creates the type for it."""
    discrepancy = _missing(KIND, "extract")
    assert _cover([discrepancy], {KIND: frozenset({"statement_runs"})}, {"statement_runs"}) == (
        [discrepancy],
        [],
    )


def test_a_label_that_an_existing_type_lacks_blocks_even_for_a_new_table() -> None:
    discrepancy = EnumDiscrepancy(STATE, ("running", "parsing"), ("running",), ("parsing",), (), ())
    assert _cover([discrepancy], {STATE: frozenset({"statement_runs"})}, {"users"}) == (
        [discrepancy],
        [],
    )


@pytest.mark.parametrize(
    "discrepancy",
    [
        EnumDiscrepancy(KIND, ("extract",), ("extract", "gone"), (), ("gone",), ()),
        EnumDiscrepancy(
            KIND,
            ("EXTRACT",),
            ("extract",),
            ("EXTRACT",),
            (),
            (("EXTRACT", "extract"),),
        ),
        EnumDiscrepancy("db_only_enum", (), ("a", "b"), (), ("a", "b"), ()),
    ],
)
def test_a_db_only_label_a_casing_drift_and_a_db_only_type_block(
    discrepancy: EnumDiscrepancy,
) -> None:
    tables = {KIND: frozenset({"statement_runs"})}
    assert _cover([discrepancy], tables, {"users"}) == ([discrepancy], [])


def test_a_missing_type_with_no_known_table_blocks() -> None:
    discrepancy = _missing("orphan_enum", "a")
    assert _cover([discrepancy], {}, {"users"}) == ([discrepancy], [])


def test_covered_and_blocking_discrepancies_are_split_in_one_call() -> None:
    new, drift = _missing(KIND, "extract"), _missing(STATE, "running")
    tables = {KIND: frozenset({"statement_runs"}), STATE: frozenset({"statements"})}
    assert _cover([new, drift], tables, {"statements"}) == (
        [drift],
        [(KIND, ("statement_runs",))],
    )


def _column(name: str, labels: tuple[str, ...]) -> SimpleNamespace:
    return SimpleNamespace(type=SimpleNamespace(name=name, enums=list(labels), native_enum=True))


def test_enum_tables_map_each_type_to_the_tables_that_use_it() -> None:
    metadata = SimpleNamespace(
        tables={
            "statement_runs": SimpleNamespace(
                columns=[
                    _column(KIND, ("extract", "classify")),
                    SimpleNamespace(type=object()),
                ]
            ),
            "public.audit": SimpleNamespace(
                columns=[
                    _column(KIND, ("extract", "classify")),
                    _column(STATE, ("running",)),
                ]
            ),
            "plain": SimpleNamespace(columns=[SimpleNamespace(type=object())]),
        }
    )
    assert enum_tables_from_metadata(metadata) == {
        KIND: frozenset({"statement_runs", "audit"}),
        STATE: frozenset({"audit"}),
    }


def test_enum_tables_read_an_array_of_an_enum() -> None:
    array = SimpleNamespace(
        item_type=SimpleNamespace(name=KIND, enums=["extract"], native_enum=True)
    )
    metadata = SimpleNamespace(tables={"t": SimpleNamespace(columns=[SimpleNamespace(type=array)])})
    assert enum_tables_from_metadata(metadata) == {KIND: frozenset({"t"})}


def test_enum_tables_reject_an_object_that_is_not_metadata() -> None:
    with pytest.raises(gate.GateNotEvaluated):
        enum_tables_from_metadata(SimpleNamespace())


def test_no_pending_migration_covers_nothing() -> None:
    """The database is already at the image's head and still lacks the table: drift, not a new table."""
    discrepancy = _missing(KIND, "extract")
    tables = {KIND: frozenset({"statement_runs"})}
    assert _cover([discrepancy], tables, {"users"}, pending=False) == (
        [discrepancy],
        [],
    )


def test_the_table_list_query_is_the_pinned_information_schema_query() -> None:
    assert gate.DB_TABLES_SQL == (
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema NOT IN ('pg_catalog', 'information_schema');"
    )


def test_enum_tables_read_a_type_decorator_over_an_enum() -> None:
    wrapped = SimpleNamespace(
        impl_instance=SimpleNamespace(name=KIND, enums=["extract"], native_enum=True)
    )
    metadata = SimpleNamespace(
        tables={"t": SimpleNamespace(columns=[SimpleNamespace(type=wrapped)])}
    )
    assert enum_tables_from_metadata(metadata) == {KIND: frozenset({"t"})}


class _Connection:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return self

    def execute(self, _query):
        return None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def run_app(monkeypatch: pytest.MonkeyPatch) -> str:
    module = ModuleType("schema_gate_run_app")
    table = SimpleNamespace(columns=[_column(KIND, ("extract", "classify"))])
    module.Base = SimpleNamespace(metadata=SimpleNamespace(tables={"statement_runs": table}))
    monkeypatch.setitem(sys.modules, "schema_gate_run_app", module)
    monkeypatch.setitem(
        sys.modules, "psycopg", SimpleNamespace(connect=lambda _url: _Connection([]))
    )
    return "schema_gate_run_app:Base.metadata"


def _run_main(metadata: str) -> int:
    return gate.main(["--metadata", metadata, "--db-url", "postgresql://db/app"])


def _stub(monkeypatch: pytest.MonkeyPatch, *, tables, pending=True) -> None:
    monkeypatch.setattr(gate, "fetch_db_tables", lambda _url: frozenset(tables))
    monkeypatch.setattr(gate, "migrations_pending", lambda *_args: pending)


def test_main_passes_when_only_a_new_table_uses_the_missing_type_and_reports_it(
    run_app: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub(monkeypatch, tables={"users"})
    rc = _run_main(run_app)
    out = capsys.readouterr().out
    assert rc == gate.EXIT_OK
    assert f"NEW_TABLE_COVERS: {KIND} (statement_runs)" in out
    assert "1 covered by new tables" in out
    assert "ROLLBACK_CLASS: A" in out


def test_main_blocks_when_the_table_already_exists_in_the_database(
    run_app: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub(monkeypatch, tables={"statement_runs"})
    assert _run_main(run_app) == gate.EXIT_BLOCKED
    assert KIND in capsys.readouterr().err


def test_main_blocks_when_no_migration_is_pending(
    run_app: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub(monkeypatch, tables={"users"}, pending=False)
    assert _run_main(run_app) == gate.EXIT_BLOCKED
    assert "NEW_TABLE_COVERS" not in capsys.readouterr().out


def test_main_blocks_and_names_the_reason_when_the_table_list_cannot_be_read(
    run_app: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def unreadable(_url):
        raise OSError("information_schema is unreadable")

    _stub(monkeypatch, tables=set())
    monkeypatch.setattr(gate, "fetch_db_tables", unreadable)
    assert _run_main(run_app) == gate.EXIT_BLOCKED
    err = capsys.readouterr().err
    assert KIND in err
    assert "NOTE: new-table coverage not applied (OSError)" in err


def test_main_blocks_and_names_the_reason_when_the_database_revision_is_unknown(
    run_app: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class CommandError(Exception):
        pass

    def unknown(*_args):
        raise CommandError("Can't locate revision identified by 'x'")

    _stub(monkeypatch, tables={"users"})
    monkeypatch.setattr(gate, "migrations_pending", unknown)
    assert _run_main(run_app) == gate.EXIT_BLOCKED
    assert "NOTE: new-table coverage not applied (CommandError)" in capsys.readouterr().err


def test_main_does_not_read_the_table_list_when_nothing_differs(
    run_app: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda _url: _Connection([(KIND, "extract"), (KIND, "classify")])),
    )

    calls: list[str] = []
    monkeypatch.setattr(gate, "fetch_db_tables", lambda url: calls.append(url) or frozenset())
    monkeypatch.setattr(gate, "migrations_pending", lambda *_args: calls.append("pending") or True)
    assert _run_main(run_app) == gate.EXIT_OK
    assert calls == []


def test_fetch_db_tables_returns_the_table_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    class Recording(_Connection):
        def execute(self, query):
            seen.append(query)

    monkeypatch.setitem(
        sys.modules,
        "psycopg",
        SimpleNamespace(connect=lambda _url: Recording([("users",), ("statements",)])),
    )
    assert gate.fetch_db_tables("postgresql://db/app") == frozenset({"users", "statements"})
    assert seen == [gate.DB_TABLES_SQL]


class _UnknownRevision(Exception):
    """Stands in for alembic's CommandError, raised for an id that no file defines."""


class _MultipleHeads(Exception):
    """Stands in for alembic's CommandError: "The script directory has multiple heads"."""


class _FakeScript:
    """alembic's ScriptDirectory. Revisions 0073 -> 0074; `head` and the prefix `0074` resolve to 0074."""

    known = {
        "0073_stmt_gin_trace_trig_perf": "0073_stmt_gin_trace_trig_perf",
        "0074_statement_runs": "0074_statement_runs",
        "0074": "0074_statement_runs",
        "head": "0074_statement_runs",
    }
    heads = ["0074_statement_runs"]
    location = ""

    @classmethod
    def from_config(cls, config):
        cls.location = config.options["script_location"]
        return cls()

    def get_revision(self, revision):
        if revision == "base":
            return None
        if revision not in self.known:
            raise _UnknownRevision(f"Can't locate revision identified by {revision!r}")
        return SimpleNamespace(revision=self.known[revision])

    def get_current_head(self):
        if len(self.heads) != 1:
            raise _MultipleHeads("The script directory has multiple heads (due to branching)")
        return self.heads[0]


class _FakeConfig:
    path = ""
    defaults: dict[str, str] = {"script_location": "migrations"}

    def __init__(self, path):
        type(self).path = path
        self.options = dict(self.defaults)

    def get_main_option(self, name):
        return self.options.get(name)

    def set_main_option(self, name, value):
        self.options[name] = value


@pytest.fixture
def fake_alembic(monkeypatch: pytest.MonkeyPatch):
    _FakeScript.heads = ["0074_statement_runs"]
    _FakeScript.known = dict(_FakeScript.known)
    monkeypatch.setitem(sys.modules, "alembic", ModuleType("alembic"))
    monkeypatch.setitem(sys.modules, "alembic.config", SimpleNamespace(Config=_FakeConfig))
    monkeypatch.setitem(sys.modules, "alembic.script", SimpleNamespace(ScriptDirectory=_FakeScript))

    def rows(revisions):
        monkeypatch.setattr(gate, "_with_libpq_cursor", lambda _url, _fn: revisions)

    return rows


def _pending(app_path="/app") -> bool:
    return gate.migrations_pending(
        "postgresql://db/app", gate.EnumSchemaSpec(metadata="m:b"), app_path
    )


def test_migrations_are_pending_when_the_database_is_behind_the_image_head(
    fake_alembic,
) -> None:
    fake_alembic(("0073_stmt_gin_trace_trig_perf",))
    assert _pending() is True


def test_no_migration_is_pending_when_the_database_is_at_the_image_head(
    fake_alembic,
) -> None:
    fake_alembic(("0074_statement_runs",))
    assert _pending() is False


def test_a_symbolic_head_row_resolves_before_the_comparison(fake_alembic) -> None:
    """A row that says `head` names the image head: nothing is pending."""
    fake_alembic(("head",))
    assert _pending() is False


def test_a_database_with_no_revision_row_is_not_treated_as_pending(
    fake_alembic,
) -> None:
    """No version row: the function returns False, so the gate blocks (alembic would run every migration)."""
    fake_alembic(())
    assert _pending() is False


def test_a_prefix_row_resolves_to_its_revision_before_the_comparison(
    fake_alembic,
) -> None:
    fake_alembic(("0074",))
    assert _pending() is False


def test_a_database_revision_that_the_image_does_not_know_raises(fake_alembic) -> None:
    fake_alembic(("0099",))
    with pytest.raises(_UnknownRevision):
        _pending()


def test_the_base_revision_raises_because_it_resolves_to_no_revision(
    fake_alembic,
) -> None:
    fake_alembic(("base",))
    with pytest.raises(ValueError, match="resolves to no revision"):
        _pending()


def test_several_version_rows_raise(fake_alembic) -> None:
    fake_alembic(("0073_stmt_gin_trace_trig_perf", "0074_statement_runs"))
    with pytest.raises(ValueError, match="holds 2 rows"):
        _pending()


def test_several_heads_in_the_image_raise(fake_alembic) -> None:
    _FakeScript.heads = ["0074a", "0074b"]
    fake_alembic(("0073_stmt_gin_trace_trig_perf",))
    with pytest.raises(_MultipleHeads, match="multiple heads"):
        _pending()


def test_a_relative_script_location_is_resolved_against_the_app_path(
    fake_alembic,
) -> None:
    fake_alembic(("0073_stmt_gin_trace_trig_perf",))
    _pending("/srv/app")
    assert _FakeScript.location == "/srv/app/migrations"


def test_the_config_file_is_the_alembic_ini_of_the_app_path(fake_alembic) -> None:
    fake_alembic(("0073_stmt_gin_trace_trig_perf",))
    _pending("/srv/app")
    assert _FakeConfig.path == "/srv/app/alembic.ini"


def test_an_alembic_ini_without_script_location_raises(
    fake_alembic, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No fallback directory: the entrypoint fails on the same file, so the gate blocks."""
    fake_alembic(("0073_stmt_gin_trace_trig_perf",))
    monkeypatch.setattr(_FakeConfig, "defaults", {})
    with pytest.raises(ValueError, match="no script_location"):
        _pending()
