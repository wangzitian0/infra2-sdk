import json
from datetime import date

import pytest

from infra2_sdk.capacity import (
    CLOUDFLARE_FREE_TIER,
    CapacityReading,
    _CapacityLimit,
    cloudflare_readings,
    evaluate,
)
from infra2_sdk.transport import HttpResponse


def test_evaluate_levels_and_unknowns() -> None:
    limits = (_CapacityLimit("a", 1000), _CapacityLimit("b", 100, "hour"), _CapacityLimit("c", 10))
    report = evaluate(limits, (CapacityReading("a", 1204), CapacityReading("b", 80)))
    by_name = {item.name: item for item in report.items}
    assert by_name["a"].level == "exceeded" and by_name["a"].ratio == pytest.approx(1.204)
    assert by_name["b"].level == "warn" and by_name["b"].window == "hour"
    assert by_name["c"].level == "unknown" and by_name["c"].ratio is None
    assert not report.ok
    assert [i.name for i in report.at_level("warn", "exceeded")] == ["a", "b"]
    assert report.to_dict()["items"][0]["ratio"] == 1.204
    assert evaluate((_CapacityLimit("a", 10),), (CapacityReading("a", 1),)).ok


def test_limit_and_reading_validation() -> None:
    with pytest.raises(ValueError, match="positive limit"):
        _CapacityLimit("a", 0)
    with pytest.raises(ValueError, match="window must be one of"):
        _CapacityLimit("a", 1, "week")
    with pytest.raises(ValueError, match="used cannot be negative"):
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


def test_cloudflare_readings_handles_errors_and_empty() -> None:
    def http_fail(*args, **kwargs) -> HttpResponse:
        return HttpResponse(500, {}, b"error")

    with pytest.raises(RuntimeError, match="HTTP 500"):
        cloudflare_readings(account="a", token="t", day=date(2026, 9, 6), transport=http_fail)

    def gql_fail(*args, **kwargs) -> HttpResponse:
        return HttpResponse(200, {}, json.dumps({"errors": ["bad query"]}).encode())

    with pytest.raises(RuntimeError, match="returned errors"):
        cloudflare_readings(account="a", token="t", day=date(2026, 9, 6), transport=gql_fail)

    def empty_accounts(*args, **kwargs) -> HttpResponse:
        return HttpResponse(200, {}, json.dumps({"data": {"viewer": {"accounts": []}}}).encode())

    assert (
        cloudflare_readings(account="a", token="t", day=date(2026, 9, 6), transport=empty_accounts)
        == ()
    )
