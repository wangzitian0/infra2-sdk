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


async def test_async_check_and_failure_evidence() -> None:
    settings = PostgresSettings("postgresql://db/app")
    result = await PostgresCheck(
        settings,
        connector=lambda *_args, **_kwargs: Connection(error=OSError("down")),
    ).probe()
    assert result.status is DependencyStatus.ABSENT
    assert "OSError: down" in result.detail


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
