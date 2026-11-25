"""Tests for pre-deploy schema & enum check gate (#698)."""

import sys
import textwrap
from enum import StrEnum
from types import ModuleType, SimpleNamespace

import pytest

from infra2_sdk.runtime import schema_check as gate
from infra2_sdk.runtime.schema_check import (
    ROLLBACK_CLASS_A,
    ROLLBACK_CLASS_C,
    check_enums,
    classify_rollback,
    compare_enum_values,
    extract_enum_values,
    query_db_enums,
)


class StatusEnum(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


def test_compare_enum_values_exact_match():
    code_vals = ["PENDING", "PROCESSING", "COMPLETED"]
    db_vals = ["PENDING", "PROCESSING", "COMPLETED"]
    disc = compare_enum_values("status", code_vals, db_vals)
    assert not disc.has_error
    assert disc.missing_in_db == ()
    assert disc.casing_mismatches == ()


def test_compare_enum_values_casing_divergence():
    # Replays #698 incident: code uses uppercase, DB uses lowercase
    code_vals = ["PENDING", "PROCESSING", "COMPLETED"]
    db_vals = ["pending", "processing", "completed"]
    disc = compare_enum_values("status", code_vals, db_vals)
    assert disc.has_error
    assert set(disc.casing_mismatches) == {
        ("PENDING", "pending"),
        ("PROCESSING", "processing"),
        ("COMPLETED", "completed"),
    }


def test_compare_enum_values_missing_in_db():
    code_vals = ["PENDING", "PROCESSING", "ARCHIVED"]
    db_vals = ["PENDING", "PROCESSING"]
    disc = compare_enum_values("status", code_vals, db_vals)
    assert disc.has_error
    assert disc.missing_in_db == ("ARCHIVED",)


def test_extract_enum_values():
    vals = extract_enum_values(StatusEnum)
    assert vals == ["PENDING", "PROCESSING", "COMPLETED", "FAILED"]


def test_check_enums_blocks_on_discrepancy():
    code_enums = {
        "status": StatusEnum,
    }
    db_enums = {
        "status": [
            "pending",
            "processing",
            "completed",
            "failed",
        ],  # lowercase mismatch!
    }
    discrepancies = check_enums(code_enums, db_enums)
    assert len(discrepancies) == 1
    assert discrepancies[0].has_error
    assert len(discrepancies[0].casing_mismatches) == 4


def test_query_db_enums_from_cursor():
    class FakeCursor:
        def fetchall(self):
            return [
                ("report_status", "draft"),
                ("report_status", "published"),
                ("user_role", "admin"),
                ("user_role", "viewer"),
            ]

    enums = query_db_enums(FakeCursor())
    assert enums["report_status"] == ["draft", "published"]
    assert enums["user_role"] == ["admin", "viewer"]


# --- Audit-hardened tests (反事实审计 2026-09-16) ---


def test_missing_in_code_is_error():
    """DB superset is dangerous: values code doesn't know → runtime deserialization failure."""
    code_vals = ["PENDING", "PROCESSING"]
    db_vals = ["PENDING", "PROCESSING", "ARCHIVED"]  # DB has more
    disc = compare_enum_values("status", code_vals, db_vals)
    assert disc.has_error  # was False before fix!
    assert disc.missing_in_code == ("ARCHIVED",)


def test_bidirectional_strict_equality():
    """Both directions must match for no error."""
    code_vals = ["A", "B", "C"]
    db_vals = ["A", "B", "C"]
    disc = compare_enum_values("status", code_vals, db_vals)
    assert not disc.has_error

    # Asymmetry in either direction is an error
    disc2 = compare_enum_values("status", ["A", "B"], ["A", "B", "C"])
    assert disc2.has_error  # missing_in_code
    disc3 = compare_enum_values("status", ["A", "B", "C"], ["A", "B"])
    assert disc3.has_error  # missing_in_db


def test_db_only_enum_detected():
    """DB enum type exists but has no code counterpart at all."""
    code_enums = {"status": ["PENDING", "DONE"]}
    db_enums = {
        "status": ["PENDING", "DONE"],
        "priority": ["low", "high"],  # DB-only, no code declaration
    }
    discrepancies = check_enums(code_enums, db_enums)
    assert len(discrepancies) == 1
    assert discrepancies[0].enum_name == "priority"
    assert discrepancies[0].has_error
    assert discrepancies[0].missing_in_code == ("low", "high")


def test_check_enums_bidirectional_with_enum_class():
    """Full integration: Enum class vs DB with superset."""
    code_enums = {"status": StatusEnum}
    db_enums = {
        "status": ["PENDING", "PROCESSING", "COMPLETED", "FAILED", "CANCELLED"],
    }
    discrepancies = check_enums(code_enums, db_enums)
    assert len(discrepancies) == 1
    assert discrepancies[0].missing_in_code == ("CANCELLED",)
    assert discrepancies[0].has_error


# --- Fail-closed on missing inputs (#718 review threads) ---


def _enum_column(name, labels, *, native=True):
    return SimpleNamespace(type=SimpleNamespace(name=name, enums=list(labels), native_enum=native))


def _metadata(*tables):
    return SimpleNamespace(
        tables={
            f"t{index}": SimpleNamespace(columns=list(columns))
            for index, columns in enumerate(tables)
        }
    )


FR_LIKE_METADATA = _metadata(
    [
        SimpleNamespace(type=SimpleNamespace()),  # a plain column: no enum
        _enum_column("account_type_enum", ["ASSET", "LIABILITY"]),
    ],
    [
        _enum_column("journal_entry_status_enum", ["draft", "posted"]),
        # the same type on a second column is one enum, not a conflict
        _enum_column("account_type_enum", ["LIABILITY", "ASSET"]),
        # native_enum=False is a VARCHAR + CHECK: no Postgres type to compare
        _enum_column("inline_check", ["a", "b"], native=False),
        SimpleNamespace(
            type=SimpleNamespace(item_type=SimpleNamespace(name="tag_enum", enums=["x"]))
        ),
    ],
)


@pytest.fixture
def fake_app(monkeypatch):
    """A registered service whose metadata module is importable."""
    module = ModuleType("schema_gate_fake_app")
    module.Base = SimpleNamespace(metadata=FR_LIKE_METADATA)
    monkeypatch.setitem(sys.modules, "schema_gate_fake_app", module)
    return ["--metadata", "schema_gate_fake_app:Base.metadata"]


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, _query):
        return None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def _fake_psycopg(monkeypatch, rows, seen_urls):
    class _Conn:
        def cursor(self):
            return _FakeCursor(rows)

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def connect(url):
        seen_urls.append(url)
        return _Conn()

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))


def test_metadata_enums_are_keyed_by_postgres_type_name() -> None:
    enums = gate.enums_from_metadata(FR_LIKE_METADATA)
    assert enums == {
        "account_type_enum": ("ASSET", "LIABILITY"),
        "journal_entry_status_enum": ("draft", "posted"),
        "tag_enum": ("x",),
    }


def test_conflicting_labels_for_one_enum_type_are_not_evaluated() -> None:
    metadata = _metadata(
        [_enum_column("status_enum", ["a", "b"])],
        [_enum_column("status_enum", ["a", "c"])],
    )
    with pytest.raises(gate.GateNotEvaluated, match="different labels"):
        gate.enums_from_metadata(metadata)


def test_the_module_path_from_718_does_not_load_and_is_not_evaluated() -> None:
    """Replays the review finding: finance_report.models.enums does not exist."""
    spec = gate.EnumSchemaSpec(metadata="finance_report.models.enums:Base.metadata")
    with pytest.raises(gate.GateNotEvaluated, match="cannot load enums from finance_report"):
        gate.load_code_enums(spec)


def test_metadata_without_native_enums_is_not_evaluated(monkeypatch) -> None:
    module = ModuleType("schema_gate_empty_app")
    module.metadata = _metadata([SimpleNamespace(type=SimpleNamespace())])
    monkeypatch.setitem(sys.modules, "schema_gate_empty_app", module)
    spec = gate.EnumSchemaSpec(metadata="schema_gate_empty_app:metadata")
    with pytest.raises(gate.GateNotEvaluated, match="no native enum types"):
        gate.load_code_enums(spec)


def test_app_path_makes_the_service_package_importable(tmp_path, monkeypatch) -> None:
    package = tmp_path / "schema_gate_pkg_app"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "registry.py").write_text(
        textwrap.dedent(
            """
            from types import SimpleNamespace
            from schema_gate_pkg_app import database
            database.Base.metadata.tables["loaded"] = SimpleNamespace(columns=[
                SimpleNamespace(type=SimpleNamespace(name="role_enum", enums=["admin"]))
            ])
            """
        ),
        encoding="utf-8",
    )
    (package / "database.py").write_text(
        textwrap.dedent(
            """
            from types import SimpleNamespace
            Base = SimpleNamespace(metadata=SimpleNamespace(tables={}))
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "path", list(sys.path))
    for name in (
        "schema_gate_pkg_app",
        "schema_gate_pkg_app.database",
        "schema_gate_pkg_app.registry",
    ):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = gate.EnumSchemaSpec(
        metadata="schema_gate_pkg_app.database:Base.metadata",
        imports=("schema_gate_pkg_app.registry",),
    )
    assert gate.load_code_enums(spec, app_path=str(tmp_path)) == {"role_enum": ("admin",)}


def test_app_path_expands_the_home_directory(tmp_path, monkeypatch) -> None:
    home = tmp_path / "home"
    (home / "app" / "schema_gate_home_app").mkdir(parents=True)
    (home / "app" / "schema_gate_home_app" / "__init__.py").write_text(
        "from types import SimpleNamespace\n"
        "metadata = SimpleNamespace(tables={'t': SimpleNamespace(columns=["
        "SimpleNamespace(type=SimpleNamespace(name='kind_enum', enums=['a']))])})\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delitem(sys.modules, "schema_gate_home_app", raising=False)
    spec = gate.EnumSchemaSpec(metadata="schema_gate_home_app:metadata")
    assert gate.load_code_enums(spec, app_path="~/app") == {"kind_enum": ("a",)}


def test_a_missing_app_path_is_not_evaluated(tmp_path, fake_app) -> None:
    with pytest.raises(gate.GateNotEvaluated, match="is not a directory"):
        gate.load_code_enums(
            gate.EnumSchemaSpec(metadata=fake_app[1]), app_path=str(tmp_path / "nope")
        )


def test_main_without_database_url_is_not_evaluated(fake_app, monkeypatch, capsys) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert gate.main([*fake_app]) == gate.EXIT_NOT_EVALUATED
    err = capsys.readouterr().err
    assert "NOT EVALUATED" in err
    assert "no database URL" in err


def test_main_with_unloadable_enums_never_touches_the_database(monkeypatch, capsys) -> None:
    seen: list[str] = []
    _fake_psycopg(monkeypatch, [], seen)
    rc = gate.main(
        ["--metadata", "schema_gate_missing_app:metadata", "--db-url", "postgresql://db/x"]
    )
    assert rc == gate.EXIT_NOT_EVALUATED
    assert seen == []
    assert "cannot load enums from schema_gate_missing_app" in capsys.readouterr().err


def test_main_reports_every_missing_input(monkeypatch, capsys) -> None:
    assert gate.main(["--metadata", "schema_gate_missing_app:metadata"]) == gate.EXIT_NOT_EVALUATED
    err = capsys.readouterr().err
    assert "no database URL" in err
    assert "cannot load enums from schema_gate_missing_app" in err


def test_main_passes_only_when_both_sides_match(fake_app, monkeypatch, capsys) -> None:
    seen: list[str] = []
    _fake_psycopg(
        monkeypatch,
        [
            ("account_type_enum", "ASSET"),
            ("account_type_enum", "LIABILITY"),
            ("journal_entry_status_enum", "draft"),
            ("journal_entry_status_enum", "posted"),
            ("tag_enum", "x"),
        ],
        seen,
    )
    # finance_report's DATABASE_URL form (credential-free here); libpq rejects "+asyncpg"
    rc = gate.main([*fake_app, "--db-url", "postgresql+asyncpg://db:5432/app"])
    assert rc == gate.EXIT_OK
    assert seen == ["postgresql://db:5432/app"]  # what reaches psycopg.connect()
    assert "3 enum types, 0 discrepancies" in capsys.readouterr().out


def test_main_blocks_on_the_698_casing_divergence(fake_app, monkeypatch, capsys) -> None:
    _fake_psycopg(
        monkeypatch,
        [
            ("account_type_enum", "asset"),
            ("account_type_enum", "liability"),
            ("journal_entry_status_enum", "draft"),
            ("journal_entry_status_enum", "posted"),
            ("tag_enum", "x"),
        ],
        [],
    )
    assert gate.main([*fake_app, "--db-url", "postgresql://db/app"]) == gate.EXIT_BLOCKED
    assert "account_type_enum" in capsys.readouterr().err


def test_main_blocks_when_the_database_is_unreachable(fake_app, monkeypatch) -> None:
    def connect(_url):
        raise OSError("connection refused")

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=connect))
    assert gate.main([*fake_app, "--db-url", "postgresql://db/app"]) == (gate.EXIT_BLOCKED)


def test_query_db_enums_rejects_an_object_that_cannot_query() -> None:
    with pytest.raises(TypeError):
        query_db_enums(object())


def test_libpq_url_drops_only_the_driver_suffix() -> None:
    assert gate.libpq_url("postgresql+asyncpg://db:5432/app") == ("postgresql://db:5432/app")
    assert gate.libpq_url("postgres+psycopg2://db/app") == "postgres://db/app"
    assert gate.libpq_url("postgresql://db/app") == "postgresql://db/app"
    assert gate.libpq_url("  postgresql+psycopg://db/app  ") == "postgresql://db/app"


# --- ROLLBACK_CLASS (Infra-022 T3.2 / TODOWRITE:20) ---


def test_classify_rollback_is_class_a_with_no_discrepancies() -> None:
    assert classify_rollback([]) == ROLLBACK_CLASS_A


def test_classify_rollback_is_class_a_when_only_code_is_ahead() -> None:
    # missing_in_db only: code declares a label the DB doesn't have yet -- purely
    # additive, old code neither reads nor needs it.
    disc = compare_enum_values("status", ["A", "B", "ARCHIVED"], ["A", "B"])
    assert disc.missing_in_db == ("ARCHIVED",)
    assert disc.missing_in_code == ()
    assert classify_rollback([disc]) == ROLLBACK_CLASS_A


def test_classify_rollback_is_class_c_for_casing_drift() -> None:
    # #698's own replay: this particular casing drift also sets missing_in_code (both
    # come off the same code_set/db_set comparison here) -- either signal alone would
    # already classify C; see the next test for a case where only casing_mismatches
    # fires and missing_in_code stays empty.
    disc = compare_enum_values("status", ["PENDING"], ["pending"])
    assert disc.casing_mismatches == (("PENDING", "pending"),)
    assert disc.missing_in_code == ("pending",)
    assert classify_rollback([disc]) == ROLLBACK_CLASS_C


def test_classify_rollback_is_class_c_for_casing_drift_alone() -> None:
    """#783 review: casing_mismatches and missing_in_code are independent signals, not
    always coupled -- code declaring BOTH case variants against a DB with only the
    lowercase one sets casing_mismatches without missing_in_code. Class C must still
    fire on casing_mismatches alone; it is never the safe tier."""
    disc = compare_enum_values("status", ["PENDING", "pending"], ["pending"])
    assert disc.casing_mismatches == (("PENDING", "pending"),)
    assert disc.missing_in_code == ()  # the counter-example: NOT set here
    assert classify_rollback([disc]) == ROLLBACK_CLASS_C


def test_classify_rollback_is_class_c_when_db_carries_orphaned_labels() -> None:
    # missing_in_code: the DB still carries a label the code no longer declares -- the
    # direction that corresponds to a DROP/RENAME having already reached the database.
    disc = compare_enum_values("status", ["A", "B"], ["A", "B", "ARCHIVED"])
    assert disc.missing_in_code == ("ARCHIVED",)
    assert classify_rollback([disc]) == ROLLBACK_CLASS_C


def test_classify_rollback_is_class_c_for_a_db_only_enum_type() -> None:
    discrepancies = check_enums(
        {"status": ["A", "B"]}, {"status": ["A", "B"], "priority": ["low", "high"]}
    )
    assert classify_rollback(discrepancies) == ROLLBACK_CLASS_C


def test_classify_rollback_is_class_a_when_mixed_with_a_clean_enum() -> None:
    # The classifier looks across the WHOLE batch: one clean enum must not mask a
    # dangerous discrepancy on another enum in the same comparison.
    clean = compare_enum_values("clean", ["X"], ["X"])
    assert not clean.has_error
    dangerous = compare_enum_values("dangerous", ["A", "B"], ["A", "B", "ARCHIVED"])
    assert classify_rollback([clean, dangerous]) == ROLLBACK_CLASS_C


def test_main_prints_rollback_class_on_a_clean_pass(fake_app, monkeypatch, capsys) -> None:
    seen: list[str] = []
    _fake_psycopg(
        monkeypatch,
        [
            ("account_type_enum", "ASSET"),
            ("account_type_enum", "LIABILITY"),
            ("journal_entry_status_enum", "draft"),
            ("journal_entry_status_enum", "posted"),
            ("tag_enum", "x"),
        ],
        seen,
    )
    rc = gate.main([*fake_app, "--db-url", "postgresql+asyncpg://db:5432/app"])
    assert rc == gate.EXIT_OK
    assert "ROLLBACK_CLASS: A" in capsys.readouterr().out


def test_main_prints_rollback_class_on_a_block(fake_app, monkeypatch, capsys) -> None:
    _fake_psycopg(
        monkeypatch,
        [
            ("account_type_enum", "asset"),
            ("account_type_enum", "liability"),
            ("journal_entry_status_enum", "draft"),
            ("journal_entry_status_enum", "posted"),
            ("tag_enum", "x"),
        ],
        [],
    )
    rc = gate.main([*fake_app, "--db-url", "postgresql://db/app"])
    assert rc == gate.EXIT_BLOCKED
    # #698's own casing-divergence replay: never the milder tier.
    assert "ROLLBACK_CLASS: C" in capsys.readouterr().out


def test_main_prints_no_rollback_class_when_not_evaluated(fake_app, monkeypatch, capsys) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = gate.main([*fake_app])
    assert rc == gate.EXIT_NOT_EVALUATED
    assert "ROLLBACK_CLASS" not in capsys.readouterr().out


# --- Declarative data preconditions & migration floor (#609) ---


@pytest.mark.parametrize(
    ("migration", "seeded", "expected_rc", "expect_err"),
    [
        ("0037", False, "EXIT_BLOCKED", True),
        ("0038", True, "EXIT_OK", False),
    ],
)
def test_data_precondition_and_migration_floor_gate(
    monkeypatch,
    capsys,
    migration: str,
    seeded: bool,
    expected_rc: str,
    expect_err: bool,
) -> None:
    """#609 AC1/AC2/AC3: precondition & migration floor gate aborts when unmet and passes once met."""
    suffix = "fail" if expect_err else "pass"
    module = ModuleType(f"schema_gate_precond_{suffix}_app")
    module.Base = SimpleNamespace(metadata=FR_LIKE_METADATA)
    monkeypatch.setitem(sys.modules, f"schema_gate_precond_{suffix}_app", module)

    class _PrecondCursor:
        def __init__(self):
            self._last_sql = ""

        def execute(self, sql):
            self._last_sql = sql

        def fetchall(self):
            if "pg_enum" in self._last_sql:
                return [
                    ("account_type_enum", "ASSET"),
                    ("account_type_enum", "LIABILITY"),
                    ("journal_entry_status_enum", "draft"),
                    ("journal_entry_status_enum", "posted"),
                    ("tag_enum", "x"),
                ]
            if "alembic_version" in self._last_sql:
                return [(migration,)]
            if "issuer_headcount_facts" in self._last_sql:
                return [(seeded,)]
            return []

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class _Conn:
        def cursor(self):
            return _PrecondCursor()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setitem(sys.modules, "psycopg", SimpleNamespace(connect=lambda _u: _Conn()))
    rc = gate.main(
        [
            "--metadata",
            f"schema_gate_precond_{suffix}_app:Base.metadata",
            "--requires-migration",
            "0037",
            "--precondition",
            "issuer_headcount_facts_seeded="
            "SELECT count(*) > 0 FROM staging.issuer_headcount_facts"
            "::run apps/data-engine/scripts/seed_issuer_headcounts.py",
            "--db-url",
            "postgresql://db/app",
        ]
    )
    assert rc == getattr(gate, expected_rc)
    captured = capsys.readouterr()
    assert f"APPLIED_MIGRATION: {migration}" in captured.out
    assert "ROLLBACK_CLASS: A" in captured.out
    if expect_err:
        assert "issuer_headcount_facts_seeded" in captured.err
        assert "seed_issuer_headcounts.py" in captured.err
    else:
        assert "FAIL" not in captured.err


def test_migration_floor_blocks_when_applied_migration_is_older_than_required() -> None:
    """#609 AC1 & AC3: requires_migration blocks when applied version < floor or non-SELECT SQL is used."""

    class _Cursor:
        def __init__(self):
            self.sql = ""

        def execute(self, sql):
            self.sql = sql

        def fetchall(self):
            return [("0035",)]

    applied, failures = gate.check_data_preconditions(
        _Cursor(),
        requires_migration="0037",
        preconditions=(("unsafe_mutation", "DELETE FROM staging.facts", "none"),),
    )
    assert applied == ("0035",)
    assert len(failures) == 2
    assert "migration_floor (0037)" in failures[0]
    assert "0035" in failures[0]
    assert "read-only SELECT statement" in failures[1]


def test_a_malformed_precondition_is_not_evaluated(fake_app, capsys) -> None:
    rc = gate.main([*fake_app, "--db-url", "postgresql://db/app", "--precondition", "no-sql"])
    assert rc == gate.EXIT_NOT_EVALUATED
    assert "--precondition must be NAME=SELECT" in capsys.readouterr().err
