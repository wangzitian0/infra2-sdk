"""Guards for StageRecord (#210)."""

from __future__ import annotations

import pytest

from infra2_sdk import StageRecord
from infra2_sdk.observability import CheckVerdict

GOOD = {
    "env": "staging.release_validation",
    "phase": "pre",
    "subject": "tag:v3.1.55",
    "ref": "refs/tags/v3.1.55",
    "event": "tag",
    "run_url": "https://github.com/o/r/actions/runs/1",
    "started_at": "2026-11-12T03:10:13Z",
    "finished_at": "2026-11-12T03:11:47Z",
    "checks": [{"name": "contract", "ok": True, "seconds": 1.5}],
    "units": ["app:finance_report", "sdk:infra2-sdk"],
    "uncovered": ["infra:redis"],
}


def test_round_trip_keeps_every_field() -> None:
    rec = StageRecord.from_dict(GOOD)
    assert rec.verdict == "pass" and rec.checks == (CheckVerdict("contract", True, seconds=1.5),)
    out = rec.to_dict()
    assert out["verdict"] == "pass" and out["schema"] == 1
    assert StageRecord.from_dict(out) == rec


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"units": ["db:x"]}, "units"),
        ({"uncovered": ["app:"]}, "uncovered"),
        ({"checks": []}, "not be empty"),
        ({"verdict": "pass", "checks": [{"name": "a", "ok": False}]}, "does not match"),
        ({"checks": [{"name": "a", "ok": True}, {"name": "a", "ok": True}]}, "unique"),
        ({"checks": [{"name": "a", "ok": True, "x": 1}]}, "unknown check"),
        ({"env": "nope"}, "unknown env"),
        ({"phase": "deploy"}, "phase"),
        ({"event": "cron"}, "event"),
        ({"subject": "branch:main"}, "subject"),
        ({"verdict": "green"}, "does not match"),
        ({"finished_at": "2026-11-12T03:10:12Z"}, "before started_at"),
        ({"extra": 1}, "unknown stage record"),
    ],
)
def test_record_refuses(patch: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        StageRecord.from_dict({**GOOD, **patch})


def test_failed_check_gives_fail_verdict() -> None:
    rec = StageRecord.from_dict({**GOOD, "checks": [{"name": "a", "ok": False}]})
    assert rec.verdict == "fail" and rec.to_dict()["verdict"] == "fail"
