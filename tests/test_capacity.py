import json
import subprocess
from datetime import date

import pytest

from infra2_sdk._transport import HttpResponse
from infra2_sdk.capacity import (
    CLOUDFLARE_FREE_TIER,
    CapacityLimit,
    CapacityReading,
    cloudflare_readings,
    evaluate,
    onepassword_capacity,
)


def test_evaluate_levels_and_unknowns() -> None:
    limits = (CapacityLimit("a", 1000), CapacityLimit("b", 100, "hour"), CapacityLimit("c", 10))
    report = evaluate(limits, (CapacityReading("a", 1204), CapacityReading("b", 80)))
    by_name = {item.name: item for item in report.items}
    assert by_name["a"].level == "exceeded" and by_name["a"].ratio == pytest.approx(1.204)
    assert by_name["b"].level == "warn" and by_name["b"].window == "hour"
    assert by_name["c"].level == "unknown" and by_name["c"].ratio is None
    assert not report.ok
    assert [i.name for i in report.at_level("warn", "exceeded")] == ["a", "b"]
    assert report.to_dict()["items"][0]["ratio"] == 1.204
    assert evaluate((CapacityLimit("a", 10),), (CapacityReading("a", 1),)).ok


def test_limit_and_reading_validation() -> None:
    with pytest.raises(ValueError):
        CapacityLimit("a", 0)
    with pytest.raises(ValueError):
        CapacityLimit("a", 1, "week")
    with pytest.raises(ValueError):
        CapacityReading("a", -1)


def test_cloudflare_readings_sum_kv_actions_and_workers() -> None:
    def transport(method, url, headers, body) -> HttpResponse:
        assert method == "POST" and headers["Authorization"] == "Bearer t"
        query = json.loads(body)
        assert query["variables"]["start"] == "2026-09-06T00:00:00Z"
        payload = {
            "data": {
                "viewer": {
                    "accounts": [
                        {
                            "kv": [
                                {"sum": {"requests": 1000}, "dimensions": {"actionType": "write"}},
                                {"sum": {"requests": 186}, "dimensions": {"actionType": "write"}},
                                {"sum": {"requests": 4984}, "dimensions": {"actionType": "read"}},
                            ],
                            "workers": [{"sum": {"requests": 4713}}],
                        }
                    ]
                }
            }
        }
        return HttpResponse(200, {}, json.dumps(payload).encode())

    readings = cloudflare_readings(
        account="acct", token="t", day=date(2026, 9, 6), transport=transport
    )
    assert readings == (
        CapacityReading("cloudflare.kv.read", 4984),
        CapacityReading("cloudflare.kv.write", 1186),
        CapacityReading("cloudflare.workers.requests", 4713),
    )
    report = evaluate(CLOUDFLARE_FREE_TIER, readings)
    assert [i.name for i in report.at_level("exceeded")] == ["cloudflare.kv.write"]


def test_onepassword_capacity_reads_limits_and_usage_from_the_cli() -> None:
    rows = [
        {
            "type": "token",
            "action": "read",
            "limit": 1000,
            "used": 120,
            "remaining": 880,
            "reset": "in 45m",
        },
        {"type": "account", "action": "write", "limit": 100, "used": 3, "reset": "in 23h"},
        {"type": "", "action": "x", "limit": 0},
    ]

    def runner(args, capture_output, text):
        assert args[:4] == ["op", "service-account", "ratelimit", "infra2-cli"]
        return subprocess.CompletedProcess(args, 0, json.dumps(rows), "")

    limits, readings = onepassword_capacity("infra2-cli", runner=runner)
    assert [(item.name, item.limit, item.window) for item in limits] == [
        ("onepassword.token.read", 1000, "hour"),
        ("onepassword.account.write", 100, "day"),
    ]
    assert readings == (
        CapacityReading("onepassword.token.read", 120),
        CapacityReading("onepassword.account.write", 3),
    )
