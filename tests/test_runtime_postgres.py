import pytest

from infra2_sdk.runtime.postgres import (
    PostgresCheck,
    PostgresSettings,
    normalize_postgres_dsn,
    probe_postgres,
)
from infra2_sdk.runtime.probes import DependencyStatus


class Connection:
    def __init__(self, *, error=None) -> None:
        self.error = error
        self.query = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query):
        if self.error:
            raise self.error
        self.query = query
        return self

    def fetchone(self):
        return (1,)


def test_dsn_normalization_and_probe() -> None:
    settings = PostgresSettings("postgresql+asyncpg://user:pass@db/app")
    assert "pass" not in repr(settings)
    assert settings.psycopg_dsn == "postgresql://user:pass@db/app"
    assert normalize_postgres_dsn("postgres://db/app") == "postgres://db/app"
    connection = Connection()
    calls = []

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return connection

    result = probe_postgres(settings, connector=connect)
    assert result.status is DependencyStatus.PRESENT
    assert connection.query == "SELECT 1"
    assert calls[0][1]["connect_timeout"] == 5


def test_postgres_settings_load_from_standard_env() -> None:
    settings = PostgresSettings.from_env(
        {
            "DATABASE_URL": "postgresql+asyncpg://user:secret@db/app",
            "DATABASE_CONNECT_TIMEOUT_SECONDS": "9",
        }
    )
    assert settings.psycopg_dsn == "postgresql://user:secret@db/app"
    assert settings.connect_timeout_seconds == 9


def test_postgres_from_env_requires_dsn_and_integer_timeout() -> None:
    with pytest.raises(ValueError, match="DATABASE_URL is required"):
        PostgresSettings.from_env({})
    with pytest.raises(ValueError, match="integer"):
        PostgresSettings.from_env(
            {"DATABASE_URL": "postgresql://db/app", "DATABASE_CONNECT_TIMEOUT_SECONDS": "5.5"}
        )


async def test_async_check_and_failure_evidence() -> None:
    settings = PostgresSettings("postgresql://db/app")
    result = await PostgresCheck(
        settings,
        connector=lambda *_args, **_kwargs: Connection(error=OSError("down")),
    ).probe()
    assert result.status is DependencyStatus.ABSENT
    assert "OSError: down" in result.detail


def test_failure_evidence_redacts_database_credentials() -> None:
    dsn = "postgresql+asyncpg://user:super-secret@db/app"

    def connect(*_args, **_kwargs):
        raise RuntimeError(f"connection failed for {dsn}")

    result = probe_postgres(PostgresSettings(dsn), connector=connect)
    assert result.status is DependencyStatus.ABSENT
    assert "super-secret" not in result.detail
    assert dsn not in result.detail
    assert "<redacted-postgres-dsn>" in result.detail


@pytest.mark.parametrize(
    "dsn,timeout,message",
    [
        ("mysql://db/app", 5, "PostgreSQL"),
        ("postgresql://db/app", 0, "between 1 and 60"),
        ("postgresql://db/app", 61, "between 1 and 60"),
    ],
)
def test_postgres_settings_validation(dsn, timeout, message) -> None:
    with pytest.raises(ValueError, match=message):
        PostgresSettings(dsn, timeout)
