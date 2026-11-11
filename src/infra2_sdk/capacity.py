"""Capacity limits, quota evaluation, sliding-window rate budgets, and Cloudflare readings."""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import date

from infra2_sdk.transport import HttpTransport, urllib_transport

_WINDOWS = ("hour", "day", "month")


@dataclass(frozen=True)
class _CapacityLimit:
    name: str
    limit: int
    window: str = "day"
    unit: str = "requests"

    def __post_init__(self) -> None:
        if not self.name or self.limit <= 0:
            raise ValueError("name and a positive limit are required")
        if self.window not in _WINDOWS:
            raise ValueError(f"window must be one of {_WINDOWS}")


@dataclass(frozen=True)
class CapacityReading:
    name: str
    used: int

    def __post_init__(self) -> None:
        if self.used < 0:
            raise ValueError("used cannot be negative")


@dataclass(frozen=True)
class _CapacityItem:
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
    items: tuple[_CapacityItem, ...]

    @property
    def ok(self) -> bool:
        return all(item.level == "ok" for item in self.items)

    def at_level(self, *levels: str) -> tuple[_CapacityItem, ...]:
        return tuple(item for item in self.items if item.level in levels)

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "items": [item.to_dict() for item in self.items]}


def evaluate(
    limits: Iterable[_CapacityLimit],
    readings: Iterable[CapacityReading],
    *,
    warn_ratio: float = 0.8,
) -> CapacityReport:
    """Pair limits with readings by name; a limit without a reading is ``unknown``."""
    used_by_name = {r.name: r.used for r in readings}
    items: list[_CapacityItem] = []
    for lim in limits:
        used = used_by_name.get(lim.name)
        level = (
            "unknown"
            if used is None
            else "exceeded"
            if used >= lim.limit
            else "warn"
            if used >= lim.limit * warn_ratio
            else "ok"
        )
        items.append(_CapacityItem(lim.name, used, lim.limit, lim.window, level))
    return CapacityReport(tuple(items))


class RateLimitWindow:
    """Thread-safe sliding-window request or token budget tracker."""

    def __init__(
        self,
        limit: int,
        window_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if limit <= 0 or window_seconds <= 0:
            raise ValueError("limit and window_seconds must be positive")
        self.limit = limit
        self.window_seconds = float(window_seconds)
        self._clock = clock
        self._events: deque[tuple[float, int]] = deque()
        self._used = 0
        self._lock = threading.Lock()

    def _evict(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._events and self._events[0][0] <= cutoff:
            _, cost = self._events.popleft()
            self._used -= cost

    def acquire(self, cost: int = 1) -> tuple[bool, float]:
        """Consume ``cost`` units when within budget; return ``(allowed, retry_after_seconds)``."""
        if cost <= 0 or cost > self.limit:
            raise ValueError("cost must be between 1 and limit")
        with self._lock:
            now = self._clock()
            self._evict(now)
            if self._used + cost <= self.limit:
                self._events.append((now, cost))
                self._used += cost
                return True, 0.0
            freed = 0
            wait_until = now + self.window_seconds
            for ts, entry_cost in self._events:
                freed += entry_cost
                if self._used - freed + cost <= self.limit:
                    wait_until = ts + self.window_seconds
                    break
            return False, max(0.0, wait_until - now)

    @property
    def used(self) -> int:
        with self._lock:
            self._evict(self._clock())
            return self._used


CLOUDFLARE_FREE_TIER = (
    _CapacityLimit("cloudflare.kv.write", 1_000),
    _CapacityLimit("cloudflare.kv.delete", 1_000),
    _CapacityLimit("cloudflare.kv.list", 1_000),
    _CapacityLimit("cloudflare.kv.read", 100_000),
    _CapacityLimit("cloudflare.workers.requests", 100_000),
)

_CF_GRAPHQL = "https://api.cloudflare.com/client/v4/graphql"
_CF_QUERY = (
    "query($account: string!, $start: Time!, $end: Time!) {"
    " viewer { accounts(filter: {accountTag: $account}) {"
    " kv: kvOperationsAdaptiveGroups("
    "limit: 500, filter: {datetime_geq: $start, datetime_leq: $end})"
    " { sum { requests } dimensions { actionType } }"
    " workers: workersInvocationsAdaptive("
    "limit: 500, filter: {datetime_geq: $start, datetime_leq: $end})"
    " { sum { requests } } } } }"
)


def cloudflare_readings(
    *, account: str, token: str, day: date, transport: HttpTransport | None = None
) -> tuple[CapacityReading, ...]:
    """Return Cloudflare KV and Workers readings for one UTC day."""
    send = transport or urllib_transport()
    iso = day.isoformat()
    variables = {"account": account, "start": f"{iso}T00:00:00Z", "end": f"{iso}T23:59:59Z"}
    resp = send(
        "POST",
        _CF_GRAPHQL,
        {"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json.dumps({"query": _CF_QUERY, "variables": variables}).encode(),
    )
    if resp.status != 200:
        raise RuntimeError(f"Cloudflare analytics failed with HTTP {resp.status}")
    payload = json.loads(resp.body.decode())
    if payload.get("errors"):
        raise RuntimeError("Cloudflare analytics returned errors")
    accounts = ((payload.get("data") or {}).get("viewer") or {}).get("accounts") or []
    if not accounts:
        return ()
    kv: dict[str, int] = {}
    for row in accounts[0].get("kv") or []:
        act = str(row["dimensions"]["actionType"])
        kv[act] = kv.get(act, 0) + int(row["sum"]["requests"])
    workers = sum(int(r["sum"]["requests"]) for r in accounts[0].get("workers") or [])
    out = [CapacityReading(f"cloudflare.kv.{act}", val) for act, val in sorted(kv.items())]
    out.append(CapacityReading("cloudflare.workers.requests", workers))
    return tuple(out)


__all__ = [
    "CLOUDFLARE_FREE_TIER",
    "CapacityReading",
    "CapacityReport",
    "RateLimitWindow",
    "cloudflare_readings",
    "evaluate",
]
