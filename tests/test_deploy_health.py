"""Tests for infra2_sdk.deploy_health — the core loop ported from finance_report's
tools/_lib/shell/health_check.sh (finance_report#1535)."""

from __future__ import annotations

import json

import pytest

from infra2_sdk.deploy_health import HealthCheckResult, poll_until_healthy


def _responses(*pairs: tuple[int, str]):
    it = iter(pairs)

    def http_get(url: str) -> tuple[int, str]:
        return next(it)

    return http_get


def test_succeeds_immediately_on_a_plain_200() -> None:
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_responses((200, '{"status": "healthy"}')),
        sleep=lambda _: None,
    )
    assert result == HealthCheckResult(attempts=1, status_code=200, body='{"status": "healthy"}')


def test_retries_through_connection_failures_then_succeeds() -> None:
    sleeps: list[float] = []
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_responses(
            (0, "connection refused"), (0, "timeout"), (200, '{"status": "healthy"}')
        ),
        sleep=sleeps.append,
        max_attempts=5,
        interval_seconds=2.0,
    )
    assert result.attempts == 3
    assert sleeps == [2.0, 2.0]


def test_retries_through_non_200_status_then_succeeds() -> None:
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_responses((404, "not found"), (200, '{"status": "healthy"}')),
        sleep=lambda _: None,
        max_attempts=5,
    )
    assert result.attempts == 2


def test_raises_after_exhausting_max_attempts() -> None:
    with pytest.raises(RuntimeError, match="did not become healthy after 2 attempts"):
        poll_until_healthy(
            "https://example.test/api/health",
            http_get=_responses((0, "unreachable"), (503, "")),
            sleep=lambda _: None,
            max_attempts=2,
        )


def test_require_status_rejects_a_200_with_the_wrong_status_field() -> None:
    with pytest.raises(RuntimeError, match="did not become healthy"):
        poll_until_healthy(
            "https://example.test/api/health",
            http_get=_responses((200, '{"status": "degraded"}')),
            require_status="healthy",
            sleep=lambda _: None,
            max_attempts=1,
        )


def test_require_status_accepts_a_different_apps_own_convention() -> None:
    # finance_report's health endpoint says "healthy"; truealpha's says "ok" — this
    # is exactly why the value isn't hardcoded (see module docstring).
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_responses((200, '{"status": "ok"}')),
        require_status="ok",
        sleep=lambda _: None,
    )
    assert result.status_code == 200


def test_require_status_none_accepts_any_200_body() -> None:
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_responses((200, "not even json")),
        sleep=lambda _: None,
    )
    assert result.body == "not even json"


def test_expected_version_matches_by_exact_git_sha() -> None:
    body = json.dumps({"status": "healthy", "git_sha": "abc1234"})
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_responses((200, body)),
        expected_version="abc1234",
        sleep=lambda _: None,
    )
    assert result.attempts == 1


def test_expected_version_matches_by_short_sha_prefix_either_direction() -> None:
    # health_check.sh's own comment: "Handle short/long SHA comparison (prefix match)".
    long_sha = json.dumps(
        {"status": "healthy", "git_sha": "abc1234def5678901234567890123456789abcd"}
    )
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_responses((200, long_sha)),
        expected_version="abc1234",
        sleep=lambda _: None,
    )
    assert result.attempts == 1

    short_sha = json.dumps({"status": "healthy", "git_sha": "abc1234"})
    result2 = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_responses((200, short_sha)),
        expected_version="abc1234def5678901234567890123456789abcd",
        sleep=lambda _: None,
    )
    assert result2.attempts == 1


def test_expected_version_falls_back_to_the_version_key() -> None:
    body = json.dumps({"status": "healthy", "version": "v1.2.3"})
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_responses((200, body)),
        expected_version="v1.2.3",
        sleep=lambda _: None,
    )
    assert result.attempts == 1


def test_version_mismatch_retries_then_succeeds_once_matching() -> None:
    old = json.dumps({"status": "healthy", "git_sha": "old0000"})
    new = json.dumps({"status": "healthy", "git_sha": "new1111"})
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_responses((200, old), (200, new)),
        expected_version="new1111",
        sleep=lambda _: None,
        max_attempts=5,
    )
    assert result.attempts == 2


def test_version_mismatch_fails_early_once_the_same_wrong_sha_is_stable() -> None:
    """Distinct from the overall attempt budget: a STABLE wrong SHA fails once its
    own smaller mismatch budget is exhausted, without waiting for max_attempts."""
    stuck = json.dumps({"status": "healthy", "git_sha": "old0000"})
    with pytest.raises(RuntimeError, match="still reporting version 'old0000'"):
        poll_until_healthy(
            "https://example.test/api/health",
            http_get=_responses((200, stuck), (200, stuck), (200, stuck)),
            expected_version="new1111",
            max_version_mismatch_attempts=2,
            sleep=lambda _: None,
            max_attempts=10,
        )


def test_version_mismatch_streak_resets_when_the_reported_version_changes() -> None:
    a = json.dumps({"status": "healthy", "git_sha": "aaaa"})
    b = json.dumps({"status": "healthy", "git_sha": "bbbb"})
    target = json.dumps({"status": "healthy", "git_sha": "cccc"})
    result = poll_until_healthy(
        "https://example.test/api/health",
        http_get=_responses((200, a), (200, b), (200, target)),
        expected_version="cccc",
        max_version_mismatch_attempts=2,
        sleep=lambda _: None,
        max_attempts=10,
    )
    assert result.attempts == 3
