"""Machine-readable delivery stages and CI gate inventory validation."""

from __future__ import annotations

import re
from importlib.resources import files
from pathlib import Path
from typing import Any

import yaml

GATE_ID_RE = re.compile(r"\A[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)+\Z")
REQUIRED_GATE_FIELDS = ("id", "stage", "task_category", "workflow", "job")


def load_delivery_stages(path: str | Path | None = None) -> dict[str, dict]:
    """Load the SDK-owned stage vocabulary as ``stage_id -> metadata``."""
    source = Path(path) if path is not None else files("infra2_sdk").joinpath("data/stages.yaml")
    with source.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    stages = data.get("stages")
    if not isinstance(stages, dict):
        raise ValueError("stages.yaml: 'stages' must be a mapping")
    return stages


def validate_gate(
    raw: dict,
    *,
    stage_ids: set[str],
    id_prefix: str | None = None,
) -> list[str]:
    errors: list[str] = []
    for field in REQUIRED_GATE_FIELDS:
        if not str(raw.get(field, "")).strip():
            errors.append(f"missing required field {field!r}")
    gate_id = str(raw.get("id", ""))
    if gate_id and not GATE_ID_RE.match(gate_id):
        errors.append(f"gate id {gate_id!r} must match {GATE_ID_RE.pattern}")
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
    """Gate an environment manifest without any infrastructure.

    Runs in an application's own CI. Errors are contract violations a deployment would
    otherwise discover late: a secret with a code default, a release value the store
    would be expected to hold, an optional field that would silently render empty. A
    store-backed field may keep a local-development default only when it is ``required``,
    ``injected`` (the deployment must supply it) or ``empty_ok``.
    """

    from infra2_sdk.runtime.config_schema import COMPOSE_SOURCES, FieldSource

    errors: list[str] = []
    declared = {field.env for field in manifest.fields}
    for field in manifest.fields:
        name = field.env
        settled = field.empty_ok or field.required or field.injected
        if field.store_backed and field.has_default and not settled:
            errors.append(f"{name}: {field.source} value must be required, injected or empty_ok")
        if field.source in COMPOSE_SOURCES and not field.injected:
            errors.append(f"{name}: {field.source} value must be injected by the deployment")
        if field.sensitive and field.source == FieldSource.CODE and not field.provided_by:
            errors.append(f"{name}: a sensitive value cannot be a code default")
        if field.source == FieldSource.BOOTSTRAP:
            errors.append(f"{name}: bootstrap values never enter an application manifest")
        for key in field.composed_keys:
            provider_key = field.provided_by.split(":", 1)[1] if field.provided_by else ""
            if key not in declared and key != provider_key:
                errors.append(f"{name}: composed_from references undeclared {key}")
    return errors
