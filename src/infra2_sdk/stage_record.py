"""One record of what a stage task produced in one phase."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from typing import Any

from infra2_sdk._wire import parse_iso_utc, reject_unknown
from infra2_sdk.ci import load_delivery_stages
from infra2_sdk.observability import CheckVerdict

__all__ = ["StageRecord"]

_SUBJECT_KINDS = ("sha", "tag", "pr", "service")
_UNIT_KINDS = ("app", "sdk", "infra", "harness")
_KEYS = {"schema", "verdict"}


def _kinded(items: Any, kinds: tuple[str, ...], name: str) -> tuple[str, ...]:
    for item in items:
        kind, _, ident = item.partition(":") if isinstance(item, str) else ("", "", "")
        if kind not in kinds or not ident:
            raise ValueError(f"{name} {item!r} must be <kind>:<id>, kind in {kinds}")
    return tuple(items)


@dataclass(frozen=True, kw_only=True)
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
        _kinded([self.subject], _SUBJECT_KINDS, "subject")
        if self.phase not in ("pre", "operation", "post"):
            raise ValueError(f"unknown phase {self.phase!r}")
        if self.event not in ("push", "tag", "dispatch", "schedule", "pull_request"):
            raise ValueError(f"unknown event {self.event!r}")
        if not (self.ref and self.run_url and self.checks):
            raise ValueError("ref, run_url and checks must not be empty")
        if parse_iso_utc(self.finished_at) < parse_iso_utc(self.started_at):
            raise ValueError("finished_at is before started_at")
        if not all(
            isinstance(c.name, str)
            and c.name
            and type(c.ok) is bool
            and type(c.seconds) in (int, float)
            and c.seconds >= 0
            for c in self.checks
        ):
            raise ValueError("a check needs a name, a bool ok and seconds >= 0")
        if len({c.name for c in self.checks}) != len(self.checks):
            raise ValueError("check ids must be unique")
        object.__setattr__(self, "checks", tuple(self.checks))
        object.__setattr__(self, "units", _kinded(self.units, _UNIT_KINDS, "units"))
        object.__setattr__(self, "uncovered", _kinded(self.uncovered, _UNIT_KINDS, "uncovered"))

    @property
    def verdict(self) -> str:
        return "pass" if all(c.ok is True for c in self.checks) else "fail"

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
        body = {k: v for k, v in data.items() if k not in _KEYS}
        try:
            rec = cls(**{**body, "checks": tuple(CheckVerdict(**c) for c in raw)})
        except TypeError as err:
            raise ValueError(str(err)) from err
        if data.get("verdict", rec.verdict) != rec.verdict:
            raise ValueError(f"verdict does not match checks: {rec.verdict}")
        return rec
