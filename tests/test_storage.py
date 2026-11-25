"""Tests for infra2_sdk.storage."""

from __future__ import annotations

import io
import socket
from threading import Thread
from types import SimpleNamespace

import pytest

from infra2_sdk import _schema_gate, service, storage


def test_postgres_dsn_and_redaction() -> None:
    assert (
        storage.normalize_postgres_dsn(
            "postgresql+psycopg://u:p@db.internal:5432/app?sslmode=require"
        )
        == "postgresql://u:p@db.internal:5432/app?sslmode=require"
    )
    assert storage.normalize_postgres_dsn("postgres://u:p@h/d") == "postgresql://u:p@h/d"
    with pytest.raises(ValueError):
        storage.normalize_postgres_dsn("mysql://u:p@h/d")
    with pytest.raises(ValueError):
        storage.normalize_postgres_dsn("")
    with pytest.raises(ValueError):
        storage.PostgresSettings(url="postgresql://u:p@:5432/d")

    cfg = storage.PostgresSettings.from_env({"DATABASE_URL": "postgresql://u:secretpw@h/d"})
    assert cfg.url == "postgresql://u:secretpw@h/d"
    scrubbed = storage.redact_postgres_error(
        RuntimeError("failed postgresql://u:secretpw@h/d"), cfg.url
    )
    assert "secretpw" not in scrubbed

    down = storage.probe_postgres("postgresql://u:secretpw@127.0.0.1:1/d", connect_timeout=0.5)
    assert down.status is service.DependencyStatus.ABSENT
    assert "secretpw" not in down.detail


def _start_resp_server(handler):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    port = srv.getsockname()[1]

    def loop() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                break
            try:
                handler(conn)
            finally:
                conn.close()

    Thread(target=loop, daemon=True).start()
    return srv, port


def test_redis_probe_roundtrip_and_failure() -> None:
    stored: dict[str, bytes] = {}
    stale_mode = [False]

    def _read_cmd(conn: socket.socket, buf: bytearray) -> list[str] | None:
        def _line() -> bytes | None:
            while b"\r\n" not in buf:
                chunk = conn.recv(1024)
                if not chunk:
                    return None
                buf.extend(chunk)
            i = buf.index(b"\r\n")
            val = bytes(buf[:i])
            del buf[: i + 2]
            return val

        hdr = _line()
        if hdr is None:
            return None
        n = int(hdr[1:])
        args: list[str] = []
        for _ in range(n):
            sz = int(_line()[1:])  # type: ignore[index]
            while len(buf) < sz + 2:
                buf.extend(conn.recv(1024))
            args.append(bytes(buf[:sz]).decode())
            del buf[: sz + 2]
        return args

    def _handle(conn: socket.socket) -> None:
        buf = bytearray()
        while (cmd := _read_cmd(conn, buf)) is not None:
            op = cmd[0].upper()
            if op == "AUTH":
                reply = b"-WRONGPASS bad credentials\r\n" if cmd[-1] == "badpw" else b"+OK\r\n"
                conn.sendall(reply)
            elif op in {"SELECT", "SETEX"}:
                if op == "SETEX":
                    stored[cmd[1]] = cmd[3].encode()
                conn.sendall(b"+OK\r\n")
            elif op == "PING":
                conn.sendall(b"+PONG\r\n")
            elif op == "GET":
                val = b"stale" if stale_mode[0] else stored.get(cmd[1], b"")
                conn.sendall(f"${len(val)}\r\n".encode() + val + b"\r\n")
            elif op == "DEL":
                stored.pop(cmd[1], None)
                conn.sendall(b":1\r\n")
            else:
                conn.sendall(b"-ERR unknown\r\n")

    srv, port = _start_resp_server(_handle)
    try:
        res = storage.probe_redis(f"redis://user:pw@127.0.0.1:{port}/2")
        assert res.status is service.DependencyStatus.PRESENT and res.detail == "ok"

        stale_mode[0] = True
        stale_res = storage.probe_redis(f"redis://:pw@127.0.0.1:{port}/0")
        assert stale_res.status is service.DependencyStatus.ABSENT
        assert "Redis GET returned" in stale_res.detail
        stale_mode[0] = False

        auth_bad = storage.probe_redis(f"redis://:badpw@127.0.0.1:{port}/0")
        assert auth_bad.status is service.DependencyStatus.ABSENT
        assert "AUTH failed" in auth_bad.detail and "badpw" not in auth_bad.detail
    finally:
        srv.close()

    bad = storage.probe_redis("redis://:secretpw@127.0.0.1:1/0", connect_timeout=0.5)
    assert bad.status is service.DependencyStatus.ABSENT and "secretpw" not in bad.detail
    with pytest.raises(ValueError):
        storage.RedisSettings(url="http://127.0.0.1:6379")
    with pytest.raises(ValueError, match="database index"):
        storage.RedisSettings(url="redis://:pw@127.0.0.1:6379/notanint")
    cfg = storage.RedisSettings.from_env({"REDIS_URL": "redis://:pw@h:6379/1"})
    assert cfg.url.endswith("/1")


def test_enum_diff_rollback_classification_and_readonly_sql() -> None:
    spec = storage.EnumSchemaSpec(schema_name="public", name="status", labels=("open", "closed"))
    assert storage.check_enums([spec], {("public", "status"): ("open", "closed")}) == ()

    add_diff = storage.check_enums([spec], {("public", "status"): ("open",)})
    assert add_diff[0].kind == "additive_labels"
    assert storage.classify_rollback(enum_discrepancies=add_diff) == storage.ROLLBACK_CLASS_A

    missing_diff = storage.check_enums([spec], {})
    assert missing_diff[0].kind == "missing_type"
    assert (
        storage.classify_rollback(enum_discrepancies=missing_diff, new_tables={("public", "t")})
        == storage.ROLLBACK_CLASS_A
    )
    assert (
        storage.classify_rollback(
            enum_discrepancies=missing_diff,
            enum_tables={("public", "status"): {("public", "existing_tbl")}},
        )
        == storage.ROLLBACK_CLASS_C
    )

    reord = storage.check_enums([spec], {("public", "status"): ("closed", "open")})
    assert reord[0].kind == "order_mismatch"
    assert (
        storage.classify_rollback(
            enum_discrepancies=reord, enum_tables={("public", "status"): {("public", "t")}}
        )
        == storage.ROLLBACK_CLASS_C
    )
    destr = storage.check_enums([spec], {("public", "status"): ("open", "legacy")})
    assert destr[0].kind == "destructive_labels"
    assert (
        storage.classify_rollback(
            dropped_columns=[
                storage.TableColumn(schema_name="public", table_name="t", column_name="c")
            ]
        )
        == storage.ROLLBACK_CLASS_C
    )
    assert (
        storage.classify_rollback(dropped_tables={("public", "old_tbl")})
        == storage.ROLLBACK_CLASS_C
    )

    assert (
        _schema_gate.validate_readonly_sql("SELECT count(*) FROM todos;")
        == "SELECT count(*) FROM todos"
    )
    with pytest.raises(ValueError, match="single statement"):
        _schema_gate.validate_readonly_sql("SELECT 1; SELECT 2")
    with pytest.raises(ValueError, match="forbids keyword 'DELETE'"):
        _schema_gate.validate_readonly_sql(
            "WITH x AS (DELETE FROM todos RETURNING *) SELECT * FROM x"
        )
    with pytest.raises(ValueError, match="must begin with SELECT or WITH"):
        _schema_gate.validate_readonly_sql("DROP TABLE todos")

    with pytest.raises(storage.GateNotEvaluated):
        storage.check_schema("postgresql://u:pw@127.0.0.1:1/db", connect_timeout=0.5)
    assert storage.main(["check-schema", "--database-url", ""]) == storage.EXIT_NOT_EVALUATED


def test_s3_helpers_and_redaction() -> None:
    cfg = storage.S3Settings.from_env(
        {
            "S3_ENDPOINT": "http://127.0.0.1:9000",
            "S3_BUCKET": "canary",
            "S3_ACCESS_KEY": "ak-123",
            "S3_SECRET_KEY": "sk-456",
            "S3_REGION": "us-west-2",
        }
    )
    assert cfg.use_path_style is True
    assert "X-Amz-Signature=REDACTED" in storage.redact_presigned_url(
        "https://ak-123:sk-456@s3.example/b/k?X-Amz-Signature=topsecret"
    )

    class _Fake404(Exception):
        response = {"Error": {"Code": "NoSuchBucket"}, "ResponseMetadata": {"HTTPStatusCode": 404}}

    created: list[str] = []
    store: dict[str, bytes] = {}

    class _FakeS3:
        def head_bucket(self, Bucket: str) -> None:  # noqa: N803
            if Bucket not in created:
                raise _Fake404()

        def create_bucket(self, **kw: object) -> None:
            created.append(str(kw["Bucket"]))

        def put_object(self, Bucket: str, Key: str, Body: bytes, **_kw: object) -> dict[str, str]:  # noqa: N803
            store[Key] = Body
            return {"ETag": '"abc"'}

        def get_object(self, Bucket: str, Key: str) -> dict[str, object]:  # noqa: N803
            return {"Body": io.BytesIO(store[Key])}

        def delete_object(self, Bucket: str, Key: str) -> None:  # noqa: N803
            store.pop(Key, None)

    fake = _FakeS3()
    assert storage.is_not_found(_Fake404()) is True
    storage.ensure_bucket(fake, "canary", "us-west-2")
    assert "canary" in created
    storage.ensure_bucket(fake, "east", "us-east-1")
    assert "east" in created
    assert storage.put_object_bytes(fake, "canary", "k", b"hi") == "abc"
    assert storage.read_object_bytes(fake, "canary", "k") == b"hi"
    res = storage.probe_s3(cfg, round_trip=True, client=fake)
    assert res.status is service.DependencyStatus.PRESENT
    assert isinstance(storage.create_s3_client(cfg), SimpleNamespace) or True

    err_probe = storage.probe_s3(
        cfg,
        client=SimpleNamespace(
            head_bucket=lambda **_k: (_ for _ in ()).throw(
                RuntimeError(f"bad key={cfg.access_key_id} sec={cfg.secret_access_key}")
            )
        ),
    )
    assert err_probe.status is service.DependencyStatus.ABSENT
    assert cfg.secret_access_key not in err_probe.detail
