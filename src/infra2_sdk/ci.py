"""Machine-readable delivery stages and CI gate inventory validation."""

from __future__ import annotations

import json
import math
import os
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from infra2_sdk._wire import _yaml, parse_iso_utc

_GATE_ID_RE = re.compile(r"\A[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\Z")
_REQUIRED_GATE_FIELDS = ("id", "stage", "task_category", "workflow", "job")

SPEC_VERSION = "1.0.0"
GATE_WALL_CLOCK_BUDGET_S = 90
SOFT_WARN_S = 75
MAX_SINGLE_TEST_S = 30
TARGET_PER_SHARD_S = 45
SHARD_MIN = 2
SHARD_MAX = 8

BANNED_IN_GATE_PATTERNS = (
    "benchmark",
    "make bench",
    "reconciliation",
    "full_recon",
    "full_reconcile",
    "soak_test",
)
BANNED_GATE_MARKERS = ("benchmark", "slow", "reconciliation", "full_recon")
GATE_EXEMPT_RE = re.compile(r"#\s*gate-exempt:\s*(\S.*)")
_JOB_KEY_RE = re.compile(r"^  ([A-Za-z_][\w-]*):\s*$")
_BLOCK_SCALAR_RE = re.compile(r":\s*[|>][+-]?\d*\s*$")


def _parse_workflow(path: str | Path) -> tuple[dict, str, str | None]:
    p = Path(path)
    if not p.is_file():
        return {}, "", None
    try:
        source = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {}, "", str(exc)
    if not source.strip():
        return {}, source, None
    yaml = _yaml()
    try:
        parsed = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        return {}, source, str(exc)
    if isinstance(parsed, dict):
        return parsed, source, None
    return {}, source, f"top level is {type(parsed).__name__}, not a mapping"


def read_workflow(path: str | Path) -> tuple[dict, str]:
    parsed, source, _ = _parse_workflow(path)
    return parsed, source


def workflow_read_error(path: str | Path) -> str | None:
    return _parse_workflow(path)[2]


def load_workflow(path: str | Path) -> dict:
    return read_workflow(path)[0]


class ScheduleError(ValueError):
    """Workflow schedule expression cannot be evaluated."""


_GAP_WINDOW_DAYS = 1461
_WINDOW_START = datetime(2024, 1, 1, tzinfo=UTC)
_NAMES = {
    "month": {
        n: i
        for i, n in enumerate(
            ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1
        )
    },
    "dow": {n: i for i, n in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))},
}
_RANGES = {"minute": (0, 59), "hour": (0, 23), "dom": (1, 31), "month": (1, 12), "dow": (0, 7)}


def _value(token: str, field: str) -> int:
    low, high = _RANGES[field]
    if token.isdigit():
        num = int(token)
        if not low <= num <= high:
            raise ScheduleError(f"{field}: {token!r} is outside {low}-{high}")
        return num
    num = _NAMES.get(field, {}).get(token.upper())
    if num is None:
        raise ScheduleError(f"{field}: {token!r} is not a number")
    return num


def _field_values(text: str, field: str) -> set[int]:
    low, high = _RANGES[field]
    out: set[int] = set()
    for elem in text.split(","):
        span, _, step_text = elem.partition("/")
        if step_text and not step_text.isdigit():
            raise ScheduleError(f"{field}: step {step_text!r} is not a number")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise ScheduleError(f"{field}: step must be at least 1")
        if span == "*":
            first, last = low, (6 if field == "dow" else high)
        elif "-" in span:
            a, _, b = span.partition("-")
            first, last = _value(a, field), _value(b, field)
        else:
            first = _value(span, field)
            last = high if step_text else first
        if first > last:
            raise ScheduleError(f"{field}: range {span!r} runs backwards")
        out.update(range(first, last + 1, step))
    return out


def largest_gap(crons: Iterable[str]) -> timedelta:
    """Return the largest gap between consecutive fire times across ``crons``."""
    listed = list(crons)
    if not listed:
        raise ScheduleError("the workflow has no cron")
    day_times: dict[int, set[int]] = {}
    for expr in listed:
        parts = expr.split()
        if len(parts) != 5:
            raise ScheduleError(f"cron {expr!r} has {len(parts)} fields, not 5")
        mins, hrs, doms, months, dows = (
            _field_values(p, f)
            for p, f in zip(parts, ("minute", "hour", "dom", "month", "dow"), strict=True)
        )
        if 7 in dows:
            dows = (dows - {7}) | {0}
        both = parts[2] != "*" and parts[4] != "*"
        cursor = _WINDOW_START
        for idx in range(_GAP_WINDOW_DAYS):
            dm, dw = cursor.day in doms, cursor.isoweekday() % 7 in dows
            if cursor.month in months and ((dm or dw) if both else (dm and dw)):
                day_times.setdefault(idx, set()).update(h * 60 + m for h in hrs for m in mins)
            cursor += timedelta(days=1)
    ordered = [d * 1440 + m for d in sorted(day_times) for m in sorted(day_times[d])]
    if len(ordered) < 2:
        raise ScheduleError("the schedule fires fewer than twice in the sample window")
    return timedelta(minutes=max(b - a for a, b in zip(ordered, ordered[1:], strict=False)))


def _exempt_lines(source: str) -> set[tuple[str, str]]:
    if not source:
        return set()
    found: set[tuple[str, str]] = set()
    job_id = current = ""
    in_jobs = False
    block_indent: int | None = None
    for line in source.splitlines():
        indent = len(line) - len(line.lstrip())
        if block_indent is not None:
            if not line.strip() or indent > block_indent:
                continue
            block_indent = None
        code = line.split("#", 1)[0]
        if code[:1] not in ("", " ", "\t"):
            in_jobs = code.startswith("jobs:")
            if not in_jobs:
                job_id = current = ""
        elif in_jobs and (job := _JOB_KEY_RE.match(code.rstrip())):
            job_id = current = job.group(1)
        stripped = code.strip()
        if stripped == "-" or stripped.startswith("- "):
            current = ""
            for key in ("- name:", "- uses:"):
                if stripped.startswith(key):
                    current = stripped[len(key) :].strip().strip("\"'")
        if _BLOCK_SCALAR_RE.search(code):
            block_indent = indent
        if job_id and current and GATE_EXEMPT_RE.search(line):
            found.add((job_id, current))
    return found


def defanged_steps(workflow: dict, job_id: str, source: str = "") -> list[str]:
    jobs = workflow.get("jobs")
    job = (jobs if isinstance(jobs, dict) else {}).get(job_id)
    if not isinstance(job, dict):
        return []
    exempt = _exempt_lines(source)
    out = ["<job>"] if job.get("continue-on-error") and (job_id, job_id) not in exempt else []
    steps = job.get("steps")
    for idx, step in enumerate(steps if isinstance(steps, list) else []):
        if isinstance(step, dict) and step.get("continue-on-error"):
            name = str(step.get("name") or step.get("uses") or f"step #{idx}")
            if (job_id, name) not in exempt:
                out.append(name)
    return out


EPSILON = 1e-6


class CoverageArtifactMissing(RuntimeError):
    """Expected Cobertura XML file is missing or malformed."""


class CoverageBaselineInvalid(RuntimeError):
    """Committed coverage baseline file is malformed."""


def read_coverage_summary(xml_path: Path) -> dict[str, float | int]:
    if not xml_path.is_file():
        raise CoverageArtifactMissing(f"required coverage artifact is missing ({xml_path})")
    try:
        root = ElementTree.parse(xml_path).getroot()
        valid, covered = int(root.attrib["lines-valid"]), int(root.attrib["lines-covered"])
    except (ElementTree.ParseError, KeyError, ValueError) as exc:
        raise CoverageArtifactMissing(f"{xml_path} is not a valid Cobertura report: {exc}") from exc
    return {
        "lines_valid": valid,
        "lines_covered": covered,
        "line_rate": covered / valid if valid else 0.0,
    }


def load_baseline(baseline_path: Path) -> dict[str, float | int] | None:
    if not baseline_path.is_file():
        return None
    try:
        data = json.loads(baseline_path.read_text(encoding="utf-8"))
        return {
            "lines_valid": int(data["lines_valid"]),
            "lines_covered": int(data["lines_covered"]),
            "line_rate": float(data["line_rate"]),
        }
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise CoverageBaselineInvalid(f"{baseline_path} is not a valid baseline: {exc}") from exc


def write_baseline(baseline_path: Path, summary: dict[str, float | int]) -> None:
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "line_rate": round(summary["line_rate"], 6),
        "lines_covered": summary["lines_covered"],
        "lines_valid": summary["lines_valid"],
    }
    baseline_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_no_regression(
    current: dict[str, float | int], baseline: dict[str, float | int] | None
) -> tuple[bool, str]:
    cur_pct = current["line_rate"] * 100
    if baseline is None:
        return True, f"no committed baseline yet; current line coverage {cur_pct:.2f}%"
    base_pct = baseline["line_rate"] * 100
    if current["line_rate"] + EPSILON < baseline["line_rate"]:
        return False, (
            f"line coverage regressed: {cur_pct:.2f}% "
            f"(covered {current['lines_covered']}/{current['lines_valid']}) < "
            f"baseline {base_pct:.2f}% "
            f"(covered {baseline['lines_covered']}/{baseline['lines_valid']})"
        )
    return True, f"line coverage {cur_pct:.2f}% >= baseline {base_pct:.2f}%"


PR_BEHIND_MAX_AGE_SECONDS = 24 * 60 * 60
_GITHUB_OUTPUT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_OUTPUT_ENV = "_".join(("GITHUB", "OUTPUT"))


def _timestamp_epoch(value: str | int | float | datetime | None) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return None
        return value.astimezone(UTC).timestamp()
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) and value > 0 else None
    if isinstance(value, str):
        try:
            parsed = parse_github_time(value)
        except ValueError:
            return None
        return parsed.timestamp() if parsed is not None else None
    return None


def is_pr_behind_fresh(
    first_behind_at: str | int | float | datetime | None,
    *,
    now: str | int | float | datetime,
    max_age_seconds: float = PR_BEHIND_MAX_AGE_SECONDS,
) -> bool:
    """Return True when base divergence started within ``max_age_seconds`` of ``now``."""
    diverged = _timestamp_epoch(first_behind_at)
    current = _timestamp_epoch(now)
    if (
        diverged is None
        or current is None
        or not math.isfinite(max_age_seconds)
        or max_age_seconds < 0
    ):
        return False
    return (current - diverged) <= max_age_seconds


def parse_github_time(value: str | None) -> datetime | None:
    return None if value is None or not value.strip() else parse_iso_utc(value, allow_naive=True)


def write_github_output(
    values: Mapping[str, str],
    *,
    output_path: str | Path | None = None,
    output_env: str = _DEFAULT_OUTPUT_ENV,
) -> None:
    resolved_path = str(output_path) if output_path is not None else os.getenv(output_env)
    if not resolved_path:
        return
    for key in values:
        if not isinstance(key, str) or not _GITHUB_OUTPUT_KEY_RE.fullmatch(key):
            raise ValueError(f"invalid GitHub output key: {key!r}")
    with open(resolved_path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            if "\n" not in value and "\r" not in value:
                print(f"{key}={value}", file=handle)
                continue
            delim = f"FINANCE_REPORT_OUTPUT_{os.urandom(16).hex()}"
            while delim in value.splitlines():
                delim = f"FINANCE_REPORT_OUTPUT_{os.urandom(16).hex()}"
            print(f"{key}<<{delim}\n{value}\n{delim}", file=handle)


def load_delivery_stages(path: str | Path | None = None) -> dict[str, dict]:
    source = Path(path) if path is not None else files("infra2_sdk").joinpath("data/stages.yaml")
    with source.open(encoding="utf-8") as handle:
        data = _yaml().safe_load(handle) or {}
    mapping = data.get("stages")
    if not isinstance(mapping, dict):
        raise ValueError("stages.yaml: 'stages' must be a mapping")
    return mapping


def validate_gate(raw: dict, *, stage_ids: set[str], id_prefix: str | None = None) -> list[str]:
    errors = [
        f"missing required field {f!r}"
        for f in _REQUIRED_GATE_FIELDS
        if not str(raw.get(f, "")).strip()
    ]
    gate_id = str(raw.get("id", ""))
    if gate_id and not _GATE_ID_RE.match(gate_id):
        errors.append(f"gate id {gate_id!r} must match {_GATE_ID_RE.pattern}")
    if id_prefix and gate_id and not gate_id.startswith(id_prefix):
        errors.append(f"gate id {gate_id!r} must carry the repo prefix {id_prefix!r}")
    stage = raw.get("stage")
    if stage and stage not in stage_ids:
        errors.append(f"unknown stage {stage!r}")
    return errors


def validate_inventory(
    gates: list[dict], *, stage_ids: set[str], id_prefix: str | None = None
) -> dict[str, object]:
    errors: list[str] = []
    seen: set[str] = set()
    for idx, gate in enumerate(gates):
        errors.extend(
            f"gate[{idx}] ({gate.get('id', '?')}): {err}"
            for err in validate_gate(gate, stage_ids=stage_ids, id_prefix=id_prefix)
        )
        gate_id = str(gate.get("id", ""))
        if gate_id in seen:
            errors.append(f"duplicate gate id {gate_id!r}")
        elif gate_id:
            seen.add(gate_id)
    return {"errors": errors, "ids": sorted(seen)}


def validate_manifest_offline(manifest: Any) -> list[str]:
    return list(manifest.validate_offline())


__all__ = [
    "BANNED_GATE_MARKERS",
    "BANNED_IN_GATE_PATTERNS",
    "CoverageArtifactMissing",
    "CoverageBaselineInvalid",
    "EPSILON",
    "GATE_EXEMPT_RE",
    "GATE_WALL_CLOCK_BUDGET_S",
    "MAX_SINGLE_TEST_S",
    "PR_BEHIND_MAX_AGE_SECONDS",
    "SHARD_MAX",
    "SHARD_MIN",
    "SOFT_WARN_S",
    "SPEC_VERSION",
    "ScheduleError",
    "TARGET_PER_SHARD_S",
    "check_no_regression",
    "defanged_steps",
    "is_pr_behind_fresh",
    "largest_gap",
    "load_baseline",
    "load_delivery_stages",
    "load_workflow",
    "parse_github_time",
    "read_coverage_summary",
    "read_workflow",
    "validate_inventory",
    "validate_manifest_offline",
    "workflow_read_error",
    "write_baseline",
    "write_github_output",
]
