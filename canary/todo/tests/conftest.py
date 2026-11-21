"""Shared helpers for the Canary Todo tests (wangzitian0/infra2-sdk#247)."""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

import psycopg
import pytest

from infra2_sdk.runtime.environment import runtime_env_contract

TODO_DIR = Path(__file__).resolve().parents[1]
TODO_APP = TODO_DIR / "app.py"

if str(TODO_DIR) not in sys.path:
    sys.path.insert(0, str(TODO_DIR))


def _contract_names() -> set[str]:
    names: set[str] = set()
    for variable in runtime_env_contract()["variables"]:
        names.add(variable["name"])
        names.update(variable["aliases"])
    return names


def clear_telemetry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every SDK contract variable, alias and OTEL_* name of the developer shell."""
    declared = _contract_names()
    for key in list(os.environ):
        if key in declared or key.startswith("OTEL_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _no_ambient_telemetry_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app starts telemetry while it loads. The developer shell must not decide that."""
    clear_telemetry_env(monkeypatch)


# The addresses a deployment gives the Canary. The app has no default for them.
DEPLOYMENT_ADDRESSES = {
    "REDIS_HOST": "platform-redis",
    "SIGNOZ_OTEL_COLLECTOR": "http://platform-signoz-otel-collector:13133",
    "OPENPANEL_API_URL": "http://platform-openpanel-api:3000/healthcheck",
    "AUTHENTIK_ENDPOINT": "http://platform-authentik-server:9000/-/health/live/",
}


@pytest.fixture(autouse=True)
def _deployment_addresses(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in DEPLOYMENT_ADDRESSES.items():
        monkeypatch.setenv(name, value)


def load_todo_module(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, TODO_APP)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTodoCursor:
    def __init__(self, rows: list[tuple] | None = None) -> None:
        self._rows = rows or []

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeTodoPgConnection:
    def __init__(self, store: FakeTodoPgStore) -> None:
        self._store = store

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def execute(self, sql: str, params: tuple = ()) -> FakeTodoCursor:
        normalized = " ".join(sql.split())
        if normalized.startswith("SELECT to_regclass"):
            return FakeTodoCursor([("todos" if self._store.table_created else None,)])
        if normalized.startswith("CREATE TABLE IF NOT EXISTS todos"):
            self._store.table_created = True
            return FakeTodoCursor()
        if normalized.startswith("SELECT id, title, completed, capability, created_at FROM todos"):
            return FakeTodoCursor(
                [
                    (
                        item["id"],
                        item["title"],
                        item["completed"],
                        item["capability"],
                        item["created_at"],
                    )
                    for item in sorted(self._store.rows, key=lambda r: r["id"])
                ]
            )
        if normalized.startswith("INSERT INTO todos"):
            title, completed, capability = params
            self._store.next_id += 1
            row = {
                "id": self._store.next_id,
                "title": title,
                "completed": bool(completed),
                "capability": capability,
                "created_at": datetime(2026, 11, 17, 5, 0, 0, tzinfo=UTC),
            }
            self._store.rows.append(row)
            return FakeTodoCursor(
                [
                    (
                        row["id"],
                        row["title"],
                        row["completed"],
                        row["capability"],
                        row["created_at"],
                    )
                ]
            )
        if normalized.startswith("UPDATE todos SET"):
            set_clause = normalized.split("UPDATE todos SET ", 1)[1].split(" WHERE id = %s", 1)[0]
            todo_id = params[-1]
            values = list(params[:-1])
            target = next((r for r in self._store.rows if r["id"] == todo_id), None)
            if target is None:
                return FakeTodoCursor([])
            idx = 0
            for assignment in (part.strip() for part in set_clause.split(",")):
                if assignment == "completed = NOT completed":
                    target["completed"] = not target["completed"]
                elif assignment == "title = %s":
                    target["title"] = values[idx]
                    idx += 1
                elif assignment == "completed = %s":
                    target["completed"] = bool(values[idx])
                    idx += 1
                elif assignment == "capability = %s":
                    target["capability"] = values[idx]
                    idx += 1
            return FakeTodoCursor(
                [
                    (
                        target["id"],
                        target["title"],
                        target["completed"],
                        target["capability"],
                        target["created_at"],
                    )
                ]
            )
        if normalized.startswith("DELETE FROM todos WHERE id = %s"):
            todo_id = params[0]
            for pos, row in enumerate(self._store.rows):
                if row["id"] == todo_id:
                    removed = self._store.rows.pop(pos)
                    return FakeTodoCursor(
                        [
                            (
                                removed["id"],
                                removed["title"],
                                removed["completed"],
                                removed["capability"],
                                removed["created_at"],
                            )
                        ]
                    )
            return FakeTodoCursor([])
        raise AssertionError(f"Unexpected SQL in fake todo DB: {sql}")


class FakeTodoPgStore:
    def __init__(self) -> None:
        self.table_created = False
        self.next_id = 0
        self.rows: list[dict] = []

    def connect(self, *args, **kwargs) -> FakeTodoPgConnection:
        return FakeTodoPgConnection(self)


def wire_fake_todo_pg(monkeypatch, store: FakeTodoPgStore | None = None) -> FakeTodoPgStore:
    pg_store = store or FakeTodoPgStore()
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://todo_app:test_todo_pg_pass_1234@platform-postgres:5432/postgres",
    )
    monkeypatch.setattr(psycopg, "connect", pg_store.connect)
    return pg_store
