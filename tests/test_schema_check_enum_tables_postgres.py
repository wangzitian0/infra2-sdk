"""`DB_ENUM_TABLES_SQL` runs on a real PostgreSQL server (#3227).

The query decides which database tables use an enum type. The dropped-table coverage trusts
that set, so each catalog shape below runs on a real server.

Server: the DSN in `INFRA2_SDK_TEST_POSTGRES_DSN`, else a throwaway `postgres:16-alpine`
container in podman on a free port of 127.0.0.1. The test never uses port 5432 and never
stops a server that it did not start. With no DSN, no podman and no `psycopg`, the module
is skipped and the skip reason names the missing item. All objects go into two new schemas
with a random name. With a DSN, the teardown removes both schemas.

### Mutation evidence

Each mutation was applied to `DB_ENUM_TABLES_SQL` with a podman server present. The named
tests failed, and the mutation was then removed.

- MA: remove the `typarray` edge. `test_the_query_finds_the_users_of_each_shape[array]`,
  `[domain_over_array]` and `[array_of_domain]` fail.
- MB: change `relkind IN ('r', 'p')` to `('r', 'p', 'v', 'm', 'f')`.
  `test_the_query_finds_the_users_of_each_shape[view_only]` and `[matview_only]` fail.
- MC: remove the `typbasetype` edge. `[domain]`, `[domain_over_domain]`,
  `[domain_over_array]`, `[array_of_domain]` and `test_a_kept_table_behind_two_domains_blocks`
  fail.
- MD: remove the `reltype` edge. `[composite_field]` and `[table_row_type]` fail.
- ME: remove the `rngsubtype` edge. `[range]` and `[multirange]` fail.

Equivalent mutant: removing `NOT a.attisdropped` from `users` changes no result, because
PostgreSQL sets `atttypid` to 0 for a dropped column. `[dropped_column]` still passes, so it
pins that server fact. The filter stays as a second guard.
"""

from __future__ import annotations

import os
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator

import pytest

from infra2_sdk.runtime import schema_check as gate

DSN_ENV = "INFRA2_SDK_TEST_POSTGRES_DSN"
IMAGE = "docker.io/library/postgres:16-alpine"

# (shape, enum type, the tables that the query must find; empty means "no entry").
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
def server_url() -> Iterator[tuple[str, bool]]:
    """``(url, owned)``: ``owned`` is True for a container that this module started."""
    psycopg = pytest.importorskip("psycopg", reason="psycopg (the postgres extra) is absent")
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
    try:
        run = subprocess.run(
            ["podman", "run", "-d", "--rm", "--name", name, "-e", f"POSTGRES_PASSWORD={password}",
             "-p", f"127.0.0.1:{port}:5432", IMAGE],
            capture_output=True, text=True, timeout=300, check=False,
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        pytest.skip("podman run did not finish in 300 s")
    if run.returncode != 0:
        pytest.skip(f"podman cannot start {IMAGE}: {run.stderr.strip()[-300:]}")
    url = f"postgresql://postgres:{password}@127.0.0.1:{port}/postgres"
    try:
        _wait_for(url, psycopg)
        yield url, True
    finally:
        subprocess.run(
            ["podman", "stop", "-t", "2", name], capture_output=True, timeout=120, check=False
        )


@pytest.fixture(scope="module")
def enum_tables(server_url: tuple[str, bool]) -> Iterator[dict[str, frozenset[str]]]:
    import psycopg

    base_url, owned = server_url
    schema = f"sdkt_{secrets.token_hex(4)}"
    s2 = f"{schema}_s2"
    with psycopg.connect(base_url, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(f"CREATE SCHEMA {s2}")
        conn.execute(f"SET search_path TO {schema}")
        conn.execute(DDL.replace("{s2}", s2))
    try:
        yield gate.fetch_db_enum_tables(base_url)
    finally:
        if not owned:
            with psycopg.connect(base_url, autocommit=True) as conn:
                conn.execute(f"DROP SCHEMA {schema}, {s2} CASCADE")


@pytest.mark.parametrize(("shape", "enum_name", "tables"), SHAPES, ids=[s[0] for s in SHAPES])
def test_the_query_finds_the_users_of_each_shape(
    enum_tables: dict[str, frozenset[str]], shape: str, enum_name: str, tables: frozenset[str]
) -> None:
    assert enum_tables.get(enum_name, frozenset()) == tables, shape


def test_the_query_sees_every_enum_type_of_the_fixture(
    enum_tables: dict[str, frozenset[str]],
) -> None:
    """A guard against an empty map: the used types must all be present."""
    used = {enum_name for _shape, enum_name, tables in SHAPES if tables}
    assert len(used) == 13
    assert used <= set(enum_tables)


def test_a_kept_table_behind_two_domains_blocks(enum_tables: dict[str, frozenset[str]]) -> None:
    """The reviewer's case: `sdkt_kept` reads the type through a domain over a domain."""
    discrepancy = gate.EnumDiscrepancy("sdkt_e_mix", (), ("a",), (), ("a",), ())
    assert gate.cover_with_dropped_tables(
        [discrepancy],
        enum_db_tables=enum_tables,
        code_enums=(),
        code_tables={"sdkt_kept"},
        migrations_pending=True,
    ) == ([discrepancy], [])
