"""Machine-readable delivery stages and CI gate inventory validation."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from infra2_sdk._wire import _yaml
from infra2_sdk.rules.compose import inspect_compose

_GATE_ID_RE = re.compile(r"\A[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\Z")
_REQUIRED_GATE_FIELDS = ("id", "stage", "task_category", "workflow", "job")

# --- Cross-repo CI testing hierarchy and compute budgets (#788, #840) --------

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

BANNED_GATE_MARKERS = (
    "benchmark",
    "slow",
    "reconciliation",
    "full_recon",
)

GATE_EXEMPT_RE = re.compile(r"#\s*gate-exempt:\s*(\S.*)")
_JOB_KEY_RE = re.compile(r"^  ([A-Za-z_][\w-]*):\s*$")
_BLOCK_SCALAR_RE = re.compile(r":\s*[|>][+-]?\d*\s*$")


def read_workflow(path: str | Path) -> tuple[dict, str]:
    """Return a workflow's parsed YAML and its raw text from a single read."""
    p = Path(path)
    if not p.is_file():
        return {}, ""
    try:
        source = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}, ""
    yaml = _yaml()
    try:
        parsed = yaml.safe_load(source)
    except yaml.YAMLError:
        return {}, source
    return (parsed if isinstance(parsed, dict) else {}), source


def workflow_read_error(path: str | Path) -> str | None:
    """Return why ``read_workflow`` gave back an empty mapping for ``path``, or None."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        source = p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return str(exc)
    if not source.strip():
        return None
    yaml = _yaml()
    try:
        parsed = yaml.safe_load(source)
    except yaml.YAMLError as exc:
        return str(exc)
    if isinstance(parsed, dict):
        return None
    return f"top level is {type(parsed).__name__}, not a mapping"


def load_workflow(path: str | Path) -> dict:
    """Return a workflow's parsed YAML, or {} when absent or unreadable."""
    return read_workflow(path)[0]


def _exempt_lines(source: str) -> set[tuple[str, str]]:
    if not source:
        return set()
    found: set[tuple[str, str]] = set()
    job_id = ""
    current = ""
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
        elif in_jobs:
            job = _JOB_KEY_RE.match(code.rstrip())
            if job:
                job_id = job.group(1)
                current = job_id
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
    """Return steps in ``job_id`` whose failure cannot fail the job, minus declared exemptions."""
    jobs = workflow.get("jobs")
    job = (jobs if isinstance(jobs, dict) else {}).get(job_id)
    if not isinstance(job, dict):
        return []
    exempt = _exempt_lines(source)
    out: list[str] = []
    if job.get("continue-on-error") and (job_id, job_id) not in exempt:
        out.append("<job>")
    steps = job.get("steps")
    for index, step in enumerate(steps if isinstance(steps, list) else []):
        if not isinstance(step, dict) or not step.get("continue-on-error"):
            continue
        name = str(step.get("name") or step.get("uses") or f"step #{index}")
        if (job_id, name) not in exempt:
            out.append(name)
    return out


# --- Cobertura XML line-coverage no-regression ratchet -----------------------

EPSILON = 1e-6


class CoverageArtifactMissing(RuntimeError):
    """The expected Cobertura XML does not exist or fails to parse."""


class CoverageBaselineInvalid(RuntimeError):
    """The committed baseline file exists but is not well-formed."""


def read_coverage_summary(xml_path: Path) -> dict[str, float | int]:
    """Extract aggregate line coverage from a Cobertura XML file's root element."""
    if not xml_path.is_file():
        raise CoverageArtifactMissing(
            f"required coverage artifact is missing (expected {xml_path}); "
            "run `pytest --cov=common --cov=tools --cov-report=xml:...` first"
        )
    try:
        root = ElementTree.parse(xml_path).getroot()
        lines_valid = int(root.attrib["lines-valid"])
        lines_covered = int(root.attrib["lines-covered"])
    except (ElementTree.ParseError, KeyError, ValueError) as exc:
        raise CoverageArtifactMissing(f"{xml_path} is not a valid Cobertura report: {exc}") from exc
    line_rate = lines_covered / lines_valid if lines_valid else 0.0
    return {
        "lines_valid": lines_valid,
        "lines_covered": lines_covered,
        "line_rate": line_rate,
    }


def load_baseline(baseline_path: Path) -> dict[str, float | int] | None:
    """Load the committed baseline, or None if it does not exist yet (first run)."""
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
    """Write the Cobertura coverage summary to ``baseline_path``."""
    baseline_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path.write_text(
        json.dumps(
            {
                "line_rate": round(summary["line_rate"], 6),
                "lines_covered": summary["lines_covered"],
                "lines_valid": summary["lines_valid"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def check_no_regression(
    current: dict[str, float | int], baseline: dict[str, float | int] | None
) -> tuple[bool, str]:
    """Return (ok, message). No baseline yet is always ok (first run establishes it)."""
    current_pct = current["line_rate"] * 100
    if baseline is None:
        return (
            True,
            f"no committed baseline yet; current line coverage {current_pct:.2f}%",
        )
    baseline_pct = baseline["line_rate"] * 100
    if current["line_rate"] + EPSILON < baseline["line_rate"]:
        return False, (
            f"line coverage regressed: {current_pct:.2f}% "
            f"(covered {current['lines_covered']}/{current['lines_valid']}) < "
            f"baseline {baseline_pct:.2f}% "
            f"(covered {baseline['lines_covered']}/{baseline['lines_valid']}). "
            "Add tests to recover the floor, or if this drop is deliberate and "
            "reviewed, re-run with --update-baseline."
        )
    return True, f"line coverage {current_pct:.2f}% >= baseline {baseline_pct:.2f}%"


# --- GitHub Actions timestamp parsing and output writing ---------------------

_GITHUB_OUTPUT_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DEFAULT_OUTPUT_ENV = "_".join(("GITHUB", "OUTPUT"))


def parse_github_time(value: str | None) -> datetime | None:
    """Parse an optional GitHub ISO timestamp as a UTC datetime."""
    if value is None or not value.strip():
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def write_github_output(
    values: Mapping[str, str],
    *,
    output_path: str | Path | None = None,
    output_env: str = _DEFAULT_OUTPUT_ENV,
) -> None:
    """Append GitHub Actions outputs when the runner exposes ``GITHUB_OUTPUT``."""
    resolved_path = str(output_path) if output_path is not None else os.getenv(output_env)
    if not resolved_path:
        return

    invalid_keys = [
        key
        for key in values
        if not isinstance(key, str) or not _GITHUB_OUTPUT_KEY_RE.fullmatch(key)
    ]
    if invalid_keys:
        raise ValueError(f"invalid GitHub output key: {invalid_keys[0]!r}")

    with open(resolved_path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            if "\n" not in value and "\r" not in value:
                print(f"{key}={value}", file=handle)
                continue

            delimiter = f"FINANCE_REPORT_OUTPUT_{os.urandom(16).hex()}"
            while delimiter in value.splitlines():
                delimiter = f"FINANCE_REPORT_OUTPUT_{os.urandom(16).hex()}"
            print(f"{key}<<{delimiter}", file=handle)
            print(value, file=handle)
            print(delimiter, file=handle)


def _load_stages_section(section: str, path: str | Path | None = None) -> dict[str, Any]:
    source = Path(path) if path is not None else files("infra2_sdk").joinpath("data/stages.yaml")
    with source.open(encoding="utf-8") as handle:
        data = _yaml().safe_load(handle) or {}
    mapping = data.get(section)
    if not isinstance(mapping, dict):
        raise ValueError(f"stages.yaml: {section!r} must be a mapping")
    return mapping


def load_delivery_stages(path: str | Path | None = None) -> dict[str, dict]:
    """Load the SDK-owned stage vocabulary as ``stage_id -> metadata``."""
    return _load_stages_section("stages", path)


def _load_pipeline_stage_deadlines(path: str | Path | None = None) -> dict[str, int]:
    """Load default pipeline stage deadlines in milliseconds from ``stages.yaml``."""
    raw = _load_stages_section("pipeline_stage_deadlines_ms", path)
    return {str(stage): int(ms) for stage, ms in raw.items()}


def validate_gate(
    raw: dict,
    *,
    stage_ids: set[str],
    id_prefix: str | None = None,
) -> list[str]:
    errors: list[str] = []
    for field in _REQUIRED_GATE_FIELDS:
        if not str(raw.get(field, "")).strip():
            errors.append(f"missing required field {field!r}")
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
    gates: list[dict],
    *,
    stage_ids: set[str],
    id_prefix: str | None = None,
) -> dict[str, object]:
    errors: list[str] = []
    seen: set[str] = set()
    for index, gate in enumerate(gates):
        for error in validate_gate(gate, stage_ids=stage_ids, id_prefix=id_prefix):
            errors.append(f"gate[{index}] ({gate.get('id', '?')}): {error}")
        gate_id = str(gate.get("id", ""))
        if gate_id in seen:
            errors.append(f"duplicate gate id {gate_id!r}")
        elif gate_id:
            seen.add(gate_id)
    return {"errors": errors, "ids": sorted(seen)}


# --------------------------------------------------------------------------- manifests


def validate_manifest_offline(manifest: Any) -> list[str]:
    """Gate an environment manifest without any infrastructure."""
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
    "SHARD_MAX",
    "SHARD_MIN",
    "SOFT_WARN_S",
    "SPEC_VERSION",
    "TARGET_PER_SHARD_S",
    "check_no_regression",
    "defanged_steps",
    "inspect_compose",
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
