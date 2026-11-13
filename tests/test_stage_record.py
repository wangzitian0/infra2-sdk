"""Guards for StageRecord (#210)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from infra2_sdk import StageRecord
from infra2_sdk.ci import load_delivery_stages
from infra2_sdk.manifests import STAGE_HOOK_PLANS
from infra2_sdk.observability import CheckVerdict, load_trail, record_verdicts

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
        ({"checks": [{"name": "a", "ok": True, "x": 1}]}, "keyword"),
        ({"env": "nope"}, "unknown env"),
        ({"phase": "deploy"}, "phase"),
        ({"event": "cron"}, "event"),
        ({"subject": "branch:main"}, "subject"),
        ({"verdict": "green"}, "does not match"),
        ({"finished_at": "2026-11-12T03:10:12Z"}, "before started_at"),
        ({"extra": 1}, "unknown stage record"),
        ({"checks": [{"name": "a", "ok": "false"}]}, "bool ok"),
        ({"checks": [{"name": "a", "ok": 1}]}, "bool ok"),
        ({"checks": [{"name": "a", "ok": True, "seconds": -5}]}, "seconds"),
        ({"checks": [{"name": "a", "ok": True, "seconds": True}]}, "seconds"),
        ({"checks": [{"ok": True}]}, "name"),
        ({"checks": [{"name": "", "ok": True}]}, "name"),
        ({"checks": [{"name": "a"}]}, "ok"),
    ],
)
def test_record_refuses(patch: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        StageRecord.from_dict({**GOOD, **patch})


def test_record_refuses_a_missing_env_with_value_error() -> None:
    body = {k: v for k, v in GOOD.items() if k != "env"}
    with pytest.raises(ValueError, match="env"):
        StageRecord.from_dict(body)


def test_check_seconds_survive_the_check_file_and_default_to_zero(tmp_path: Path) -> None:
    file = tmp_path / "checks.jsonl"
    record_verdicts(file, source="smoke", checks=[CheckVerdict("a", False, "x", seconds=2.5)])
    with file.open("a", encoding="utf-8") as fh:  # an old line has no seconds key
        fh.write(json.dumps({"source": "gate", "checks": [{"name": "b", "ok": False}]}) + "\n")
    trail = load_trail(file, expected_sources=["smoke", "gate"])
    assert (trail.failing["a"].seconds, trail.failing["b"].seconds) == (2.5, 0.0)


def test_every_plan_stage_id_is_a_stage_of_stages_yaml() -> None:
    stages = load_delivery_stages()
    assert {n: p.stage_id for n, p in STAGE_HOOK_PLANS.items()} == {
        "preview": "preview.runtime",
        "staging": "staging.release_validation",
        "prod": "prod.release_integrity",
        "schedule": "staging.provider_regression",
    }
    assert all(p.stage_id in stages for p in STAGE_HOOK_PLANS.values())


def test_failed_check_gives_fail_verdict() -> None:
    rec = StageRecord.from_dict({**GOOD, "checks": [{"name": "a", "ok": False}]})
    assert rec.verdict == "fail" and rec.to_dict()["verdict"] == "fail"
