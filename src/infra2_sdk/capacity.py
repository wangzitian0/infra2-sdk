"""Capacity limits and readings: the quota side of every external dependency.

A limit is a number per window; a reading is what was used. ``evaluate`` turns them into
levels a daily check can act on. Collectors for open APIs (Cloudflare analytics, the
1Password rate-limit command) live here so that no consumer re-implements the query.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import date

from infra2_sdk._transport import HttpTransport, urllib_transport

WINDOWS = ("hour", "day", "month")
LEVELS = ("ok", "warn", "exceeded", "unknown")


@dataclass(frozen=True)
class CapacityLimit:
    name: str
    limit: int
    window: str = "day"
    unit: str = "requests"

    def __post_init__(self) -> None:
        if not self.name or self.limit <= 0:
            raise ValueError("name and a positive limit are required")
        if self.window not in WINDOWS:
            raise ValueError(f"window must be one of {WINDOWS}")


@dataclass(frozen=True)
class CapacityReading:
    name: str
    used: int

    def __post_init__(self) -> None:
        if self.used < 0:
            raise ValueError("used cannot be negative")


@dataclass(frozen=True)
class CapacityItem:
    name: str
    used: int | None
    limit: int
    window: str
    level: str

    @property
    def ratio(self) -> float | None:
        return None if self.used is None else self.used / self.limit

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["ratio"] = None if self.ratio is None else round(self.ratio, 4)
        return data


@dataclass(frozen=True)
class CapacityReport:
    items: tuple[CapacityItem, ...]

    @property
    def ok(self) -> bool:
        return all(item.level == "ok" for item in self.items)

    def at_level(self, *levels: str) -> tuple[CapacityItem, ...]:
        return tuple(item for item in self.items if item.level in levels)

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "items": [item.to_dict() for item in self.items]}


def evaluate(
    limits: Iterable[CapacityLimit],
    readings: Iterable[CapacityReading],
    *,
    warn_ratio: float = 0.8,
) -> CapacityReport:
    """Pair limits with readings by name. A limit without a reading is ``unknown``."""

    used_by_name = {reading.name: reading.used for reading in readings}
    items: list[CapacityItem] = []
    for limit in limits:
        used = used_by_name.get(limit.name)
        if used is None:
            level = "unknown"
        elif used >= limit.limit:
            level = "exceeded"
        elif used >= limit.limit * warn_ratio:
            level = "warn"
        else:
            level = "ok"
        items.append(CapacityItem(limit.name, used, limit.limit, limit.window, level))
    return CapacityReport(tuple(items))


# --------------------------------------------------------------------------- known limits

CLOUDFLARE_FREE_TIER = (
    CapacityLimit("cloudflare.kv.write", 1_000),
    CapacityLimit("cloudflare.kv.delete", 1_000),
    CapacityLimit("cloudflare.kv.list", 1_000),
    CapacityLimit("cloudflare.kv.read", 100_000),
    CapacityLimit("cloudflare.workers.requests", 100_000),
)

# --------------------------------------------------------------------------- collectors

_CF_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
_CF_QUERY = """
query($account: string!, $start: Time!, $end: Time!) {
  viewer { accounts(filter: {accountTag: $account}) {
    kv: kvOperationsAdaptiveGroups(
      limit: 500, filter: {datetime_geq: $start, datetime_leq: $end}
    ) { sum { requests } dimensions { actionType } }
    workers: workersInvocationsAdaptive(
      limit: 500, filter: {datetime_geq: $start, datetime_leq: $end}
    ) { sum { requests } }
  } }
}
"""


def cloudflare_readings(
    *,
    account: str,
    token: str,
    day: date,
    transport: HttpTransport | None = None,
) -> tuple[CapacityReading, ...]:
    """KV operations by action and Workers invocations for one UTC day."""

    send = transport or urllib_transport()
    variables = {
        "account": account,
        "start": f"{day.isoformat()}T00:00:00Z",
        "end": f"{day.isoformat()}T23:59:59Z",
    }
    response = send(
        "POST",
        _CF_GRAPHQL,
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json.dumps({"query": _CF_QUERY, "variables": variables}).encode("utf-8"),
    )
    if response.status != 200:
        raise RuntimeError(f"Cloudflare analytics failed with HTTP {response.status}")
    payload = json.loads(response.body.decode("utf-8"))
    if payload.get("errors"):
        raise RuntimeError("Cloudflare analytics returned errors")
    accounts = payload.get("data", {}).get("viewer", {}).get("accounts", [])
    if not accounts:
        return ()
    kv: dict[str, int] = {}
    for row in accounts[0].get("kv", []):
        action = str(row["dimensions"]["actionType"])
        kv[action] = kv.get(action, 0) + int(row["sum"]["requests"])
    workers = sum(int(row["sum"]["requests"]) for row in accounts[0].get("workers", []))
    readings = [
        CapacityReading(f"cloudflare.kv.{action}", used) for action, used in sorted(kv.items())
    ]
    readings.append(CapacityReading("cloudflare.workers.requests", workers))
    return tuple(readings)


Runner = Callable[..., subprocess.CompletedProcess[str]]


def onepassword_capacity(
    service_account: str, *, runner: Runner = subprocess.run
) -> tuple[tuple[CapacityLimit, ...], tuple[CapacityReading, ...]]:
    """Limits and usage from ``op service-account ratelimit``; the CLI reports both."""

    result = runner(
        ["op", "service-account", "ratelimit", service_account, "--format=json"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("op service-account ratelimit failed")
    rows = json.loads(result.stdout or "[]")
    limits: list[CapacityLimit] = []
    readings: list[CapacityReading] = []
    for row in rows:
        kind = str(row.get("type", "")).lower()
        action = str(row.get("action", "")).lower()
        limit = int(row.get("limit", 0))
        if not (kind and action) or limit <= 0:
            continue
        name = f"onepassword.{kind}.{action}"
        reset = str(row.get("reset", "")).lower()
        window = (
            "hour"
            if kind == "token" or "h" in reset and "m" in reset and "d" not in reset
            else "day"
        )
        limits.append(CapacityLimit(name, limit, window))
        readings.append(CapacityReading(name, int(row.get("used", 0))))
    return tuple(limits), tuple(readings)
