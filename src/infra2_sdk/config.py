"""Environment tiers, standard runtime keys, configuration manifests, and startup checks."""

from __future__ import annotations

import argparse
import difflib
import functools
import hashlib
import json
import os
import re
import sys
from collections.abc import Collection, Mapping, Sequence
from enum import StrEnum
from importlib.resources import files as _resource_files
from pathlib import Path
from typing import Any, Final, get_args

import yaml
from pydantic import BaseModel, SecretStr, field_validator, model_validator

from infra2_sdk._core import (
    ContractModel,
    canonical_sha256,
    load_dotted_symbol,
    require_contract_version,
)
from infra2_sdk._core import (
    is_preview_alias as _is_preview_alias,
)

_MANIFEST_CONTRACT_VERSION: Final[int] = 2
_RETIRED_ENV_NAMES: Final[frozenset[str]] = frozenset({"app_env", "image_tag", "service_name"})


class EnvironmentTier(StrEnum):
    LOCAL_DEV = "local_dev"
    LOCAL_TEST = "local_test"
    GITHUB_CI = "github_ci"
    PREVIEW = "preview"
    STAGING = "staging"
    PRODUCTION = "production"


class RuntimeEnvKey(StrEnum):
    ENVIRONMENT = "ENVIRONMENT"
    SERVICE_NAME = "OTEL_SERVICE_NAME"
    SERVICE_VERSION = "SERVICE_VERSION"
    GIT_COMMIT_SHA = "GIT_COMMIT_SHA"
    IMAGE_DIGEST = "IMAGE_DIGEST"
    CONFIGURATION_SHA256 = "CONFIGURATION_SHA256"
    RELEASE_ID = "RELEASE_ID"
    OTEL_EXPORTER_OTLP_ENDPOINT = "OTEL_EXPORTER_OTLP_ENDPOINT"
    OTEL_RESOURCE_ATTRIBUTES = "OTEL_RESOURCE_ATTRIBUTES"


class _FieldSource(StrEnum):
    BOOTSTRAP = "bootstrap"
    HUMAN = "human"
    RUNTIME = "runtime"
    RELEASE = "release"
    DECISION = "decision"
    CODE = "code"


_STORE_BACKED_SOURCES = frozenset({_FieldSource.HUMAN, _FieldSource.RUNTIME})
_COMPOSE_SOURCES = frozenset({_FieldSource.RELEASE, _FieldSource.DECISION})
_FIELD_SCOPES = frozenset({"env", "project"})
_CI_POLICIES = frozenset({"forbidden", "allowed"})
_PROVIDED_BY_RE = re.compile(r"\A[a-z][a-z0-9_]*/[a-z][a-z0-9_-]*:[A-Za-z_][A-Za-z0-9_]*\Z")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_ENV_PLACEHOLDER_RE = re.compile(r"\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_IDENT_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")
_ENV_VAR_RE = re.compile(r"\A[A-Z][A-Z0-9_]*\Z")
_RENDER_KEYS = (
    "source",
    "provided_by",
    "composed_from",
    "store_key",
    "empty_ok",
    "scope",
    "sensitive",
    "required",
)

_LOCAL_DEV_VALUES = frozenset({"dev", "development", "local", "local_dev"})
_LOCAL_TEST_VALUES = frozenset({"test", "testing", "ci", "local_ci", "local_test"})
_PRODUCTION_VALUES = frozenset({"prod", "production"})


def resolve_tier(value: str | EnvironmentTier, *, github_actions: bool = False) -> EnvironmentTier:
    """Resolve a raw environment string or tier enum into a canonical EnvironmentTier."""
    if isinstance(value, EnvironmentTier):
        return value
    if not isinstance(value, str):
        raise TypeError("environment must be a string or EnvironmentTier")
    cleaned = value.strip().lower()
    normalized = cleaned.replace("-", "_")
    if normalized in _LOCAL_DEV_VALUES:
        return EnvironmentTier.LOCAL_DEV
    if normalized in _LOCAL_TEST_VALUES:
        return EnvironmentTier.GITHUB_CI if github_actions else EnvironmentTier.LOCAL_TEST
    if normalized == EnvironmentTier.GITHUB_CI.value:
        return EnvironmentTier.GITHUB_CI
    if normalized == EnvironmentTier.PREVIEW.value or _is_preview_alias(cleaned):
        return EnvironmentTier.PREVIEW
    if normalized == EnvironmentTier.STAGING.value:
        return EnvironmentTier.STAGING
    if normalized in _PRODUCTION_VALUES:
        return EnvironmentTier.PRODUCTION
    raise ValueError(f"unknown environment: {value!r}")


def read_runtime_env(
    key: RuntimeEnvKey,
    environ: Mapping[str, str] | None = None,
    *,
    default: str = "",
    required: bool = False,
) -> str:
    """Read one canonical runtime variable from ``environ``."""
    canonical = RuntimeEnvKey(key)
    values = os.environ if environ is None else environ
    raw = values.get(canonical.value)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if required:
        raise ValueError(f"{canonical.value} is required")
    return default


@functools.cache
def _cached_shared_env_contract() -> dict[str, Any]:
    source = _resource_files("infra2_sdk").joinpath("data/shared_env.yaml")
    data = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict) or not isinstance(data.get("groups"), dict):
        raise ValueError("shared_env.yaml: 'groups' must be a mapping")
    return data


def _check_shared_env(manifest: EnvironmentManifest) -> list[str]:
    """Validate manifest field env names against ``shared_env.yaml`` and retired aliases."""
    contract = _cached_shared_env_contract()
    canonical_by_lower: dict[str, tuple[str, bool]] = {}
    for entries in contract["groups"].values():
        for entry in entries:
            canonical = str(entry["name"])
            canonical_by_lower[canonical.lower()] = (canonical, bool(entry.get("sensitive", False)))

    issues: list[str] = []
    for fld in manifest.fields:
        if fld.env.lower() in _RETIRED_ENV_NAMES:
            issues.append(f"{fld.env}: retired environment variable name")
        if not _ENV_VAR_RE.fullmatch(fld.env):
            issues.append(f"{fld.env}: must be uppercase POSIX identifier")
        matched = canonical_by_lower.get(fld.env.lower())
        if matched is not None:
            canonical, expected_sensitive = matched
            if fld.env != canonical:
                issues.append(f"{fld.env}: use canonical spelling {canonical}")
            if expected_sensitive and not fld.sensitive:
                issues.append(f"{fld.env}: shared variable must be marked sensitive=True")
    return issues


class EnvironmentField(ContractModel):
    """One environment variable declared by an application settings model."""

    field: str
    env: str
    aliases: tuple[str, ...] = ()
    required: bool = False
    injected: bool = False
    has_default: bool = True
    sensitive: bool = False
    group: str = "Application"
    description: str = ""
    source: _FieldSource = _FieldSource.CODE
    empty_ok: bool = False
    scope: str = "env"
    provided_by: str = ""
    composed_from: str = ""
    store_key: str = ""
    ci: str = "forbidden"

    @model_validator(mode="after")
    def _validate_field(self) -> EnvironmentField:
        if not self.field or not self.env:
            raise ValueError("field and env must be non-empty")
        names = (self.env, *self.aliases)
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate environment alias for {self.field}")
        if self.scope not in _FIELD_SCOPES:
            raise ValueError(f"{self.field}: scope must be one of {sorted(_FIELD_SCOPES)}")
        if self.ci not in _CI_POLICIES:
            raise ValueError(f"{self.field}: ci must be one of {sorted(_CI_POLICIES)}")
        if self.provided_by and not _PROVIDED_BY_RE.match(self.provided_by):
            raise ValueError(f"{self.field}: provided_by must look like project/service:KEY")
        if self.required and self.empty_ok:
            raise ValueError(f"{self.field}: a required field cannot be empty_ok")
        if self.store_key and not _IDENT_RE.fullmatch(self.store_key):
            raise ValueError(f"{self.field}: store_key must be an identifier")
        if self.composed_from and not (
            _PLACEHOLDER_RE.search(self.composed_from)
            or _ENV_PLACEHOLDER_RE.search(self.composed_from)
        ):
            raise ValueError(
                f"{self.field}: composed_from needs a {{KEY}} or {{env:NAME}} placeholder"
            )
        return self

    @property
    def store_backed(self) -> bool:
        return self.source in _STORE_BACKED_SOURCES and not self.provided_by

    @property
    def composed_keys(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_PLACEHOLDER_RE.findall(self.composed_from)))

    @property
    def composed_env(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_ENV_PLACEHOLDER_RE.findall(self.composed_from)))


def _is_secret_annotation(annotation: Any) -> bool:
    if annotation is None:
        return False
    if annotation is SecretStr or (
        isinstance(annotation, type) and issubclass(annotation, SecretStr)
    ):
        return True
    name = getattr(annotation, "__name__", "")
    if isinstance(name, str) and "secret" in name.lower():
        return True
    return any(_is_secret_annotation(arg) for arg in get_args(annotation))


class EnvironmentManifest(ContractModel):
    """Versioned application configuration contract."""

    source: str
    fields: tuple[EnvironmentField, ...]
    contract_version: int = _MANIFEST_CONTRACT_VERSION

    @field_validator("contract_version", mode="before")
    @classmethod
    def _check_version(cls, value: Any) -> int:
        return require_contract_version(
            value, _MANIFEST_CONTRACT_VERSION, label="environment manifest version"
        )

    @model_validator(mode="after")
    def _validate_manifest(self) -> EnvironmentManifest:
        if not self.source:
            raise ValueError("source is required")
        owners: dict[str, str] = {}
        for fld in self.fields:
            for env_name in (fld.env, *fld.aliases):
                if (owner := owners.get(env_name)) is not None:
                    raise ValueError(
                        f"environment variable {env_name!r} is shared "
                        f"by {owner!r} and {fld.field!r}"
                    )
                owners[env_name] = fld.field
        return self

    def store_backed(self) -> tuple[EnvironmentField, ...]:
        return tuple(f for f in self.fields if f.store_backed)

    def validate_offline(self) -> list[str]:
        """Return contract violations in this manifest without external infrastructure."""
        errors: list[str] = _check_shared_env(self)
        declared = {f.env for f in self.fields}
        for f in self.fields:
            settled = f.empty_ok or f.required or f.injected
            if f.store_backed and f.has_default and not settled:
                errors.append(
                    f"{f.env}: {f.source.value} value must be required, injected or empty_ok"
                )
            if f.source in _COMPOSE_SOURCES and not f.injected:
                errors.append(f"{f.env}: {f.source.value} value must be injected by the deployment")
            if (
                f.sensitive
                and f.source is _FieldSource.CODE
                and not f.provided_by
                and not f.composed_from
            ):
                errors.append(f"{f.env}: a sensitive value cannot be a code default")
            if f.source is _FieldSource.BOOTSTRAP:
                errors.append(f"{f.env}: bootstrap values never enter an application manifest")
            provider_key = f.provided_by.split(":", 1)[1] if f.provided_by else ""
            own_key = f.store_key if f.store_key and f.store_backed and not f.empty_ok else ""
            for key in f.composed_keys:
                if key not in declared and key not in (provider_key, own_key):
                    errors.append(f"{f.env}: composed_from references undeclared {key}")
            for env_ref in f.composed_env:
                if not _ENV_VAR_RE.fullmatch(env_ref):
                    errors.append(f"{f.env}: composed_from env:{env_ref} must be uppercase POSIX")
        return errors

    def fingerprint(self, environ: Mapping[str, str]) -> str:
        """Compute deterministic SHA-256 fingerprint across configured manifest values."""
        lines: list[str] = []
        for f in sorted(self.fields, key=lambda item: item.env):
            raw = environ.get(f.env)
            if isinstance(raw, str) and raw.strip():
                val = raw if f.sensitive else raw.strip()
                lines.append(f"{f.env}={hashlib.sha256(val.encode()).hexdigest()}")
        return canonical_sha256(lines)

    @classmethod
    def from_file(cls, path: str | Path) -> EnvironmentManifest:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(raw)

    @classmethod
    def from_model(
        cls,
        model: type[Any],
        *,
        source: str | None = None,
        overrides: Mapping[str, Mapping[str, object]] | None = None,
    ) -> EnvironmentManifest:
        """Extract an EnvironmentManifest from a Pydantic v2 BaseModel."""
        if not (isinstance(model, type) and issubclass(model, BaseModel)):
            raise TypeError("model must be a Pydantic BaseModel subclass")
        cfg = model.model_config
        prefix = cfg.get("env_prefix", "")
        prefix = prefix if isinstance(prefix, str) else ""
        fields: list[EnvironmentField] = []
        for name, info in model.model_fields.items():
            raw_alias = info.validation_alias
            val_aliases = (
                [raw_alias]
                if isinstance(raw_alias, str)
                else [str(c) for c in getattr(raw_alias, "choices", ()) if isinstance(c, str)]
            )
            canonical = val_aliases[0] if val_aliases else f"{prefix}{info.alias or name}".upper()
            extra = info.json_schema_extra if isinstance(info.json_schema_extra, Mapping) else {}
            f_source = _FieldSource(str(extra.get("source") or _FieldSource.CODE.value))
            required = bool(info.is_required())
            fields.append(
                EnvironmentField(
                    field=str(name),
                    env=str(canonical),
                    required=required,
                    injected=bool(extra.get("injected") or f_source in _COMPOSE_SOURCES),
                    has_default=not required,
                    sensitive=(
                        bool(extra.get("sensitive")) or _is_secret_annotation(info.annotation)
                    ),
                    group=str(extra.get("group", "Application")),
                    description=str(info.description or ""),
                    source=f_source,
                    empty_ok=bool(extra.get("empty_ok", False)),
                    scope=str(extra.get("scope") or "env"),
                    provided_by=str(extra.get("provided_by") or ""),
                    composed_from=str(extra.get("composed_from") or ""),
                    ci=str(extra.get("ci") or "forbidden"),
                    store_key=str(extra.get("store_key") or ""),
                )
            )
        if overrides:
            by_name = {f.field: idx for idx, f in enumerate(fields)}
            if unknown := sorted(set(overrides) - set(by_name)):
                raise ValueError(f"overrides name unknown settings fields: {unknown}")
            for name, override in overrides.items():
                changes = dict(override)
                if str(changes.get("source", "")) in {s.value for s in _COMPOSE_SOURCES}:
                    changes.setdefault("injected", True)
                merged_field = {**fields[by_name[name]].model_dump(), **changes}
                fields[by_name[name]] = EnvironmentField.model_validate(merged_field)
        return cls(
            source=source or f"{model.__module__}.{model.__qualname__}",
            fields=tuple(fields),
        )

    @classmethod
    def merge(
        cls,
        manifests: Sequence[tuple[str, EnvironmentManifest]],
        *,
        source: str,
        exclude: Collection[str] = (),
    ) -> EnvironmentManifest:
        """Merge several manifests in declaration order, failing on field metadata drift."""
        excluded = set(exclude)
        fields: dict[str, EnvironmentField] = {}
        for label, manifest in manifests:
            for entry in manifest.fields:
                if entry.env in excluded:
                    continue
                existing = fields.get(entry.env)
                if existing is None:
                    fields[entry.env] = entry
                    continue
                for key in _RENDER_KEYS:
                    if getattr(existing, key) != getattr(entry, key):
                        raise ValueError(
                            f"{source}: {entry.env}.{key} declared differently in {label}"
                        )
        return cls(source=source, fields=tuple(fields.values()))


def assert_env(
    manifest: EnvironmentManifest | str | Path,
    environ: Mapping[str, str] | None = None,
    *,
    require_injected: bool = False,
) -> None:
    """Fail closed when ``environ`` misses required or injected variables."""
    if isinstance(manifest, EnvironmentManifest):
        loaded = manifest
        label = loaded.source
    else:
        path = Path(manifest)
        if not path.is_file():
            raise RuntimeError(f"environment manifest {path} is missing")
        loaded = EnvironmentManifest.from_file(path)
        label = str(path)
    env = os.environ if environ is None else environ
    missing = [
        fld.env
        for fld in loaded.fields
        if (fld.required or (require_injected and fld.injected))
        and not (isinstance(val := env.get(fld.env), str) and (fld.empty_ok or val.strip()))
    ]
    if missing:
        names = ", ".join(sorted(missing))
        raise RuntimeError(f"{label}: missing {len(missing)} variable(s): {names}")


def _handle_export_or_check(args: argparse.Namespace, *, check_only: bool) -> int:
    manifest = EnvironmentManifest.from_model(
        load_dotted_symbol(args.settings, require_type=True), source=args.source
    )
    if offline_errors := manifest.validate_offline():
        for err in offline_errors:
            print(err, file=sys.stderr)
        return 1
    text = manifest.to_json()
    path: Path = args.json
    current = path.read_text(encoding="utf-8") if path.exists() else ""
    if current == text:
        return 0
    if check_only:
        sys.stdout.writelines(
            difflib.unified_diff(
                current.splitlines(keepends=True),
                text.splitlines(keepends=True),
                fromfile=str(path),
                tofile=f"{path} (generated)",
            )
        )
        print("1 generated file(s) are stale; run `export` to update")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return 0


def _main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint: ``python -m infra2_sdk.config {export|check|validate} ...``."""
    parser = argparse.ArgumentParser(prog="python -m infra2_sdk.config")
    sub = parser.add_subparsers(dest="command", required=True)

    for cmd_name in ("export", "check"):
        sp = sub.add_parser(cmd_name)
        sp.add_argument("settings", help="settings class as module:Class")
        sp.add_argument("--source", default=None, help="manifest source label")
        sp.add_argument("--json", type=Path, required=True, help="manifest JSON path")

    vp = sub.add_parser("validate")
    vp.add_argument("manifest", type=Path, help="env.manifest.json path")
    vp.add_argument("--no-require-injected", action="store_true")

    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command in ("export", "check"):
        return _handle_export_or_check(args, check_only=(args.command == "check"))

    try:
        assert_env(args.manifest, require_injected=not args.no_require_injected)
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("environment ok")
    return 0


def __dir__() -> list[str]:
    return sorted(__all__)


__all__ = (
    "EnvironmentField",
    "EnvironmentManifest",
    "EnvironmentTier",
    "RuntimeEnvKey",
    "assert_env",
    "read_runtime_env",
    "resolve_tier",
)

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
