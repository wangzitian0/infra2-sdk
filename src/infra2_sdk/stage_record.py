"""One record of what a stage task produced in one phase."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from typing import Any

from infra2_sdk._wire import parse_iso_utc, reject_unknown
from infra2_sdk.ci import load_delivery_stages
from infra2_sdk.observability import CheckVerdict

PHASES = ("pre", "operation", "post")
EVENTS = ("push", "tag", "dispatch", "schedule", "pull_request")
SUBJECT_KINDS = ("sha", "tag", "pr", "service")
UNIT_KINDS = ("app", "sdk", "infra", "harness")
_KEYS = {"schema", "verdict"}


def _kinded(items: Any, kinds: tuple[str, ...], name: str) -> tuple[str, ...]:
    for item in items:
        kind, _, ident = item.partition(":") if isinstance(item, str) else ("", "", "")
        if kind not in kinds or not ident:
            raise ValueError(f"{name} {item!r} must be '<kind>:<id>', kind in {kinds}")
    return tuple(items)


@dataclass(frozen=True)
class StageRecord:
    env: str
    phase: str
    subject: str
    ref: str
    event: str
    run_url: str
    started_at: str
    finished_at: str
    checks: tuple[CheckVerdict, ...]
    units: tuple[str, ...] = ()
    uncovered: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.env not in load_delivery_stages():
            raise ValueError(f"unknown env {self.env!r}")
        object.__setattr__(self, "subject", _kinded([self.subject], SUBJECT_KINDS, "subject")[0])
        for name, allowed, val in (("phase", PHASES, self.phase), ("event", EVENTS, self.event)):
            if val not in allowed:
                raise ValueError(f"{name} {val!r} not in {allowed}")
        if not self.ref or not self.run_url:
            raise ValueError("ref and run_url must be non-empty")
        if parse_iso_utc(self.finished_at) < parse_iso_utc(self.started_at):
            raise ValueError("finished_at is before started_at")
        if not self.checks:
            raise ValueError("checks must not be empty")
        if len({c.name for c in self.checks}) != len(self.checks):
            raise ValueError("check ids must be unique")
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "units", _kinded(self.units, UNIT_KINDS, "units"))
        object.__setattr__(self, "uncovered", _kinded(self.uncovered, UNIT_KINDS, "uncovered"))

    @property
    def verdict(self) -> str:
        return "pass" if all(c.ok for c in self.checks) else "fail"

    def to_dict(self) -> dict[str, Any]:
        return {"schema": 1, "verdict": self.verdict, **asdict(self)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> StageRecord:
        if not isinstance(data, Mapping):
            raise ValueError("stage record must be a JSON object")
        reject_unknown(data, {f.name for f in fields(cls)} | _KEYS, "stage record")
        if data.get("schema", 1) != 1:
            raise ValueError("unsupported stage record schema")
        raw = data.get("checks")
        if not isinstance(raw, (list, tuple)) or not all(isinstance(c, Mapping) for c in raw):
            raise ValueError("checks must be a list of objects")
        for c in raw:
            reject_unknown(c, {f.name for f in fields(CheckVerdict)}, "check")
        body = {k: v for k, v in data.items() if k not in _KEYS}
        rec = cls(**{**body, "checks": tuple(CheckVerdict(**c) for c in raw)})
        if data.get("verdict", rec.verdict) != rec.verdict:
            raise ValueError(f"verdict does not match checks: {rec.verdict}")
        return rec
