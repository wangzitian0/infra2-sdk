"""Real PostgreSQL catalog tests for infra2_sdk.storage enum & schema gate."""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest

from infra2_sdk import _schema_gate, storage

DSN_ENV = "INFRA2_SDK_TEST_POSTGRES_DSN"
IMAGE = "docker.io/library/postgres:16-alpine"

SHAPES: list[tuple[str, str, frozenset[str]]] = [
    ("direct", "sdkt_e_direct", frozenset({"sdkt_direct"})),
    ("array", "sdkt_e_array", frozenset({"sdkt_array"})),
    ("domain", "sdkt_e_domain", frozenset({"sdkt_domain"})),
    ("domain_over_domain", "sdkt_e_domdom", frozenset({"sdkt_domdom"})),
    ("domain_over_array", "sdkt_e_domarr", frozenset({"sdkt_domarr"})),
    ("array_of_domain", "sdkt_e_arrdom", frozenset({"sdkt_arrdom"})),
    ("composite_field", "sdkt_e_comp", frozenset({"sdkt_comp"})),
    ("table_row_type", "sdkt_e_row", frozenset({"sdkt_row", "sdkt_rowuser"})),
    ("range", "sdkt_e_range", frozenset({"sdkt_range"})),
    ("multirange", "sdkt_e_multi", frozenset({"sdkt_multi"})),
    ("partitioned", "sdkt_e_part", frozenset({"sdkt_part", "sdkt_part_a"})),
    ("other_schema", "sdkt_e_s2", frozenset({"sdkt_s2"})),
    ("two_domains_mix", "sdkt_e_mix", frozenset({"sdkt_gone", "sdkt_kept"})),
    ("view_only", "sdkt_e_view", frozenset()),
    ("matview_only", "sdkt_e_matview", frozenset()),
    ("function_only", "sdkt_e_func", frozenset()),
    ("unused", "sdkt_e_unused", frozenset()),
    ("dropped_column", "sdkt_e_dropped", frozenset()),
]

DDL = """
CREATE TYPE sdkt_e_direct AS ENUM ('a');
CREATE TABLE sdkt_direct (v sdkt_e_direct);
CREATE TYPE sdkt_e_array AS ENUM ('a');
CREATE TABLE sdkt_array (v sdkt_e_array[]);
CREATE TYPE sdkt_e_domain AS ENUM ('a');
CREATE DOMAIN sdkt_d_domain AS sdkt_e_domain;
CREATE TABLE sdkt_domain (v sdkt_d_domain);
CREATE TYPE sdkt_e_domdom AS ENUM ('a');
CREATE DOMAIN sdkt_d_domdom_in AS sdkt_e_domdom;
CREATE DOMAIN sdkt_d_domdom_out AS sdkt_d_domdom_in;
CREATE TABLE sdkt_domdom (v sdkt_d_domdom_out);
CREATE TYPE sdkt_e_domarr AS ENUM ('a');
CREATE DOMAIN sdkt_d_domarr AS sdkt_e_domarr[];
CREATE TABLE sdkt_domarr (v sdkt_d_domarr);
CREATE TYPE sdkt_e_arrdom AS ENUM ('a');
CREATE DOMAIN sdkt_d_arrdom AS sdkt_e_arrdom;
CREATE TABLE sdkt_arrdom (v sdkt_d_arrdom[]);
CREATE TYPE sdkt_e_comp AS ENUM ('a');
CREATE TYPE sdkt_c_comp AS (x sdkt_e_comp);
CREATE TABLE sdkt_comp (v sdkt_c_comp);
CREATE TYPE sdkt_e_row AS ENUM ('a');
CREATE TABLE sdkt_row (v sdkt_e_row);
CREATE TABLE sdkt_rowuser (r sdkt_row);
CREATE TYPE sdkt_e_range AS ENUM ('a', 'b');
CREATE TYPE sdkt_r_range AS RANGE (subtype = sdkt_e_range);
CREATE TABLE sdkt_range (v sdkt_r_range);
CREATE TYPE sdkt_e_multi AS ENUM ('a', 'b');
CREATE TYPE sdkt_r_multi AS RANGE (subtype = sdkt_e_multi, multirange_type_name = sdkt_mr_multi);
CREATE TABLE sdkt_multi (v sdkt_mr_multi);
CREATE TYPE sdkt_e_part AS ENUM ('a');
CREATE TABLE sdkt_part (v sdkt_e_part) PARTITION BY LIST (v);
CREATE TABLE sdkt_part_a PARTITION OF sdkt_part FOR VALUES IN ('a');
CREATE TYPE sdkt_e_s2 AS ENUM ('a');
CREATE TABLE {s2}.sdkt_s2 (v sdkt_e_s2);
CREATE TYPE sdkt_e_mix AS ENUM ('a');
CREATE TABLE sdkt_gone (v sdkt_e_mix);
CREATE DOMAIN sdkt_d_mix_in AS sdkt_e_mix;
CREATE DOMAIN sdkt_d_mix_out AS sdkt_d_mix_in;
CREATE TABLE sdkt_kept (v sdkt_d_mix_out);
CREATE TYPE sdkt_e_view AS ENUM ('a');
CREATE VIEW sdkt_view AS SELECT 'a'::sdkt_e_view AS x;
CREATE TYPE sdkt_e_matview AS ENUM ('a');
CREATE MATERIALIZED VIEW sdkt_matview AS SELECT 'a'::sdkt_e_matview AS x;
CREATE TYPE sdkt_e_func AS ENUM ('a');
CREATE FUNCTION sdkt_func(sdkt_e_func) RETURNS int LANGUAGE sql AS 'SELECT 1';
CREATE TYPE sdkt_e_unused AS ENUM ('a');
CREATE TYPE sdkt_e_dropped AS ENUM ('a');
CREATE TABLE sdkt_dropped (id int, v sdkt_e_dropped);
ALTER TABLE sdkt_dropped DROP COLUMN v;
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(url: str, psycopg) -> None:
    deadline = time.monotonic() + 90
    while True:
        try:
            with psycopg.connect(url, connect_timeout=3) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError:
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


@pytest.fixture(scope="module")
def pg_url() -> Iterator[tuple[str, bool]]:
    psycopg = pytest.importorskip("psycopg", reason="psycopg is absent")
    dsn = os.environ.get(DSN_ENV, "")
    if dsn:
        _wait_for(dsn, psycopg)
        yield dsn, False
        return
    if shutil.which("podman") is None:
        pytest.skip(f"no PostgreSQL: set {DSN_ENV} or install podman")
    port = _free_port()
    name = f"sdkt-{secrets.token_hex(4)}"
    password = secrets.token_hex(8)
    run = subprocess.run(
        [
            "podman",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-e",
            f"POSTGRES_PASSWORD={password}",
            "-p",
            f"127.0.0.1:{port}:5432",
            IMAGE,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if run.returncode != 0:
        pytest.skip(f"podman cannot start {IMAGE}: {run.stderr.strip()[-200:]}")
    url = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
    try:
        _wait_for(url, psycopg)
        yield url, True
    finally:
        subprocess.run(
            ["podman", "stop", "-t", "2", name], capture_output=True, timeout=120, check=False
        )


@pytest.fixture(scope="module")
def enum_tables(pg_url: tuple[str, bool]) -> Iterator[tuple[str, str, dict[str, frozenset[str]]]]:
    import psycopg

    base_url, owned = pg_url
    schema = f"sdkt_{secrets.token_hex(4)}"
    s2 = f"{schema}_s2"
    with psycopg.connect(base_url, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"CREATE SCHEMA {s2}")
        conn.execute(f"SET search_path TO {schema}")
        conn.execute(DDL.replace("{s2}", s2))
    try:
        with psycopg.connect(base_url) as conn, conn.cursor() as cur:
            raw = _schema_gate.fetch_db_enum_tables(cur)
        by_enum = {
            enum_name: frozenset(t for _, t in tables)
            for (_es, enum_name), tables in raw.items()
            if enum_name.startswith("sdkt_")
        }
        yield base_url, schema, by_enum
    finally:
        if not owned:
            with psycopg.connect(base_url, autocommit=True) as conn:
                conn.execute(f"DROP SCHEMA {schema}, {s2} CASCADE")


@pytest.mark.parametrize(("shape", "enum_name", "tables"), SHAPES, ids=[s[0] for s in SHAPES])
def test_enum_tables_query_shapes(
    enum_tables: tuple[str, str, dict[str, frozenset[str]]],
    shape: str,
    enum_name: str,
    tables: frozenset[str],
) -> None:
    _url, _schema, mapping = enum_tables
    assert mapping.get(enum_name, frozenset()) == tables, shape


def test_probe_and_check_schema_live(
    enum_tables: tuple[str, str, dict[str, frozenset[str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    url, schema, _ = enum_tables
    probe = storage.probe_postgres(url)
    assert probe.detail == "ok" and probe.status.value == "present"
    report = storage.check_schema(
        url,
        expected_enums=[
            storage.EnumSchemaSpec(schema_name=schema, name="sdkt_e_direct", labels=("a",))
        ],
        preconditions=["SELECT 1 = 1"],
    )
    assert report.is_blocking is False and report.rollback_class == storage.ROLLBACK_CLASS_A

    bad_pre = storage.check_schema(url, preconditions=["SELECT false"])
    assert bad_pre.is_blocking is True and bad_pre.preconditions_passed is False

    tbl_rep = storage.check_schema(
        url,
        expected_tables={(schema, "sdkt_direct"): {"v": "sdkt_e_direct"}},
        preconditions=["SELECT 1 = 1"],
    )
    assert tbl_rep.rollback_class == storage.ROLLBACK_CLASS_C

    monkeypatch.syspath_prepend("canary/todo")
    rc = storage.main(
        [
            "check-schema",
            "--database-url",
            url,
            "--metadata",
            "canary_schema:METADATA",
            "--label",
            "canary/todo",
            "--json",
        ]
    )
    assert rc in {storage.EXIT_OK, storage.EXIT_BLOCKED}
