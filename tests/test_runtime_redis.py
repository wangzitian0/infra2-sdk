from __future__ import annotations

import socket
import threading

import pytest

from infra2_sdk.runtime.health import DependencyStatus
from infra2_sdk.runtime.redis import RedisSettings, probe_redis, redact_redis_error


class _FakeRedisServer:
    """Minimal synchronous TCP RESP server for Redis probe unit tests."""

    def __init__(
        self,
        *,
        password: str = "",
        ping_reply: bytes = b"+PONG\r\n",
        get_reply: bytes | None = None,
        truncate_bulk: bool = False,
    ) -> None:
        self.password = password
        self.ping_reply = ping_reply
        self.get_reply = get_reply
        self.truncate_bulk = truncate_bulk
        self.store: dict[str, str] = {}
        self.commands: list[list[str]] = []
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._sock.close()

    def _read_cmd(self, rf) -> list[str] | None:
        header = rf.readline()
        if not header:
            return None
        if not header.startswith(b"*"):
            raise ValueError(f"unexpected RESP header: {header!r}")
        count = int(header[1:].strip())
        parts: list[str] = []
        for _ in range(count):
            size_line = rf.readline()
            size = int(size_line[1:].strip())
            payload = rf.read(size)
            rf.read(2)
            parts.append(payload.decode("utf-8"))
        return parts

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        authed = not self.password
        with conn, conn.makefile("rb") as rf:
            while True:
                try:
                    cmd = self._read_cmd(rf)
                except (OSError, ValueError):
                    return
                if not cmd:
                    return
                self.commands.append(cmd)
                op = cmd[0].upper()
                if op == "AUTH":
                    supplied = cmd[-1]
                    if supplied == self.password:
                        authed = True
                        conn.sendall(b"+OK\r\n")
                    else:
                        conn.sendall(b"-WRONGPASS invalid username-password pair\r\n")
                elif not authed:
                    conn.sendall(b"-NOAUTH Authentication required.\r\n")
                elif op == "PING":
                    conn.sendall(self.ping_reply)
                elif op == "SELECT":
                    conn.sendall(b"+OK\r\n")
                elif op == "SETEX":
                    self.store[cmd[1]] = cmd[3]
                    conn.sendall(b"+OK\r\n")
                elif op == "GET":
                    if self.truncate_bulk:
                        conn.sendall(b"$10\r\nabc")
                        return
                    if self.get_reply is not None:
                        conn.sendall(self.get_reply)
                    else:
                        val = self.store.get(cmd[1])
                        if val is None:
                            conn.sendall(b"$-1\r\n")
                        else:
                            raw = val.encode("utf-8")
                            conn.sendall(b"$%d\r\n%s\r\n" % (len(raw), raw))
                elif op == "DEL":
                    removed = 1 if self.store.pop(cmd[1], None) is not None else 0
                    conn.sendall(b":%d\r\n" % removed)
                else:
                    conn.sendall(b"-ERR unknown command\r\n")


def test_redis_settings_validation_and_repr() -> None:
    s = RedisSettings(url="rediss://user:top%2Dsecret@cache.internal:6380/2")
    assert s.host == "cache.internal"
    assert s.port == 6380
    assert s.username == "user"
    assert s.password == "top-secret"
    assert s.db == 2
    assert s.use_tls is True
    assert "top" not in repr(s)

    for bad in ("", "http://redis:6379", "redis://", "redis://host/notint", "redis://host:99999/0"):
        with pytest.raises(ValueError):
            RedisSettings(url=bad)
    with pytest.raises(ValueError, match="connect_timeout_seconds"):
        RedisSettings(url="redis://127.0.0.1:6379/0", connect_timeout_seconds=True)
    with pytest.raises(ValueError, match="socket_timeout_seconds"):
        RedisSettings(url="redis://127.0.0.1:6379/0", socket_timeout_seconds=0.05)


def test_probe_redis_ping_and_write_roundtrip() -> None:
    srv = _FakeRedisServer(password="pw-secret")
    try:
        s = RedisSettings(url=f"redis://:pw-secret@127.0.0.1:{srv.port}/1")
        ping_only = probe_redis(s, write_probe=False)
        assert ping_only.status is DependencyStatus.PRESENT
        assert ping_only.detail == "PING succeeded"
        assert [c[0] for c in srv.commands] == ["AUTH", "SELECT", "PING"]

        srv.commands.clear()
        rw = probe_redis(s, write_probe=True, key_prefix="test:probe:", ttl_seconds=5)
        assert rw.status is DependencyStatus.PRESENT
        assert "SETEX/GET/DEL verified" in rw.detail
        assert [c[0] for c in srv.commands] == ["AUTH", "SELECT", "PING", "SETEX", "GET", "DEL"]
        assert srv.store == {}
    finally:
        srv.close()


def test_probe_redis_fails_closed_on_wrongpass_truncated_bulk_and_scrubs_errors() -> None:
    srv = _FakeRedisServer(password="right-secret")
    try:
        bad = RedisSettings(url=f"redis://:wrong-secret@127.0.0.1:{srv.port}/0")
        res = probe_redis(bad, write_probe=True)
        assert res.status is DependencyStatus.ABSENT
        assert "WRONGPASS" in res.detail
        assert "wrong-secret" not in res.detail
    finally:
        srv.close()

    trunc = _FakeRedisServer(truncate_bulk=True)
    try:
        s = RedisSettings(url=f"redis://127.0.0.1:{trunc.port}/0")
        res = probe_redis(s, write_probe=True)
        assert res.status is DependencyStatus.ABSENT
        assert "Redis closed the connection" in res.detail
    finally:
        trunc.close()

    scrubbed = redact_redis_error(
        "dial redis://admin:s3cr3t@10.0.0.2:6379/0 failed alongside s3cr3t and other rediss://u:p@h:6380/1",
        "redis://admin:s3cr3t@10.0.0.2:6379/0",
    )
    assert "s3cr3t" not in scrubbed
    assert "u:p@" not in scrubbed
    assert "<redacted-redis-url>" in scrubbed
    assert "rediss://<redacted>@" in scrubbed
