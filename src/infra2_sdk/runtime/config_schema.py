"""Open JSON Schema contracts for environment-provided application settings."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any

JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
ENVIRONMENT_MANIFEST_VERSION = 1


@dataclass(frozen=True)
class EnvironmentField:
    """One environment variable exposed by an application settings model."""

    field: str
    env: str
    aliases: tuple[str, ...] = ()
    required: bool = False
    injected: bool = False
    has_default: bool = True
    sensitive: bool = False
    group: str = "Application"
    description: str = ""

    def __post_init__(self) -> None:
        if not self.field or not self.env:
            raise ValueError("field and env must be non-empty")
        names = (self.env, *self.aliases)
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate environment alias for {self.field}")

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["aliases"] = list(self.aliases)
        return data

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> EnvironmentField:
        aliases = raw.get("aliases", [])
        if not isinstance(aliases, list) or any(not isinstance(value, str) for value in aliases):
            raise ValueError("aliases must be an array of strings")
        return cls(
            field=_string(raw, "field"),
            env=_string(raw, "env"),
            aliases=tuple(aliases),
            required=_boolean(raw, "required"),
            injected=_boolean(raw, "injected"),
            has_default=_boolean(raw, "has_default", default=True),
            sensitive=_boolean(raw, "sensitive"),
            group=_string(raw, "group", required=False) or "Application",
            description=_string(raw, "description", required=False),
        )


@dataclass(frozen=True)
class EnvironmentManifest:
    """Versioned App-to-platform configuration injection contract."""

    source: str
    fields: tuple[EnvironmentField, ...]
    contract_version: int = ENVIRONMENT_MANIFEST_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != ENVIRONMENT_MANIFEST_VERSION:
            raise ValueError(f"unsupported environment manifest version {self.contract_version}")
        if not self.source:
            raise ValueError("source is required")
        owners: dict[str, str] = {}
        for field in self.fields:
            for env_name in (field.env, *field.aliases):
                owner = owners.get(env_name)
                if owner is not None:
                    raise ValueError(
                        f"environment variable {env_name!r} is shared by {owner!r} "
                        f"and {field.field!r}"
                    )
                owners[env_name] = field.field

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "source": self.source,
            "fields": [field.to_dict() for field in self.fields],
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> EnvironmentManifest:
        try:
            contract_version = int(raw.get("contract_version", 0))
        except (TypeError, ValueError):
            raise ValueError("contract_version must be an integer") from None
        fields = raw.get("fields")
        if not isinstance(fields, list) or any(not isinstance(value, Mapping) for value in fields):
            raise ValueError("fields must be an array of objects")
        return cls(
            contract_version=contract_version,
            source=_string(raw, "source"),
            fields=tuple(EnvironmentField.from_dict(value) for value in fields),
        )

    def to_json_schema(self) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for field in self.fields:
            item: dict[str, Any] = {
                "type": "string",
                "title": field.field,
                "x-env-aliases": list(field.aliases),
                "x-injected": field.injected,
                "x-has-default": field.has_default,
                "x-sensitive": field.sensitive,
                "x-group": field.group,
            }
            if field.description:
                item["description"] = field.description
            properties[field.env] = item
            if field.required:
                required.append(field.env)
        schema: dict[str, Any] = {
            "$schema": JSON_SCHEMA_DIALECT,
            "title": self.source,
            "type": "object",
            "additionalProperties": True,
            "properties": properties,
        }
        if required:
            schema["required"] = required
        return schema


@dataclass(frozen=True)
class EnvironmentValidation:
    missing: tuple[str, ...]
    resolved: Mapping[str, str]

    @property
    def valid(self) -> bool:
        return not self.missing


def settings_json_schema(model: type, *, title: str | None = None) -> dict[str, Any]:
    """Return a JSON Schema 2020-12 document from a Pydantic-compatible model."""

    renderer = getattr(model, "model_json_schema", None)
    if not callable(renderer):
        raise TypeError("model must expose model_json_schema()")
    try:
        rendered = renderer(by_alias=True, mode="validation")
    except TypeError:
        rendered = renderer(by_alias=True)
    if not isinstance(rendered, Mapping):
        raise TypeError("model_json_schema() must return a mapping")
    schema = deepcopy(dict(rendered))
    schema["$schema"] = JSON_SCHEMA_DIALECT
    if title is not None:
        schema["title"] = title
    return schema


def environment_manifest_from_model(
    model: type, *, source: str | None = None
) -> EnvironmentManifest:
    """Extract an injection manifest from Pydantic v2 field metadata by duck typing."""

    model_fields = getattr(model, "model_fields", None)
    if not isinstance(model_fields, Mapping):
        raise TypeError("model must expose Pydantic v2 model_fields")
    model_config = getattr(model, "model_config", {})
    env_prefix = model_config.get("env_prefix", "") if isinstance(model_config, Mapping) else ""
    if not isinstance(env_prefix, str):
        env_prefix = ""
    fields: list[EnvironmentField] = []
    for name, info in model_fields.items():
        validation_aliases = list(_validation_aliases(getattr(info, "validation_alias", None)))
        canonical = (
            validation_aliases[0]
            if validation_aliases
            else f"{env_prefix}{getattr(info, 'alias', None) or name}".upper()
        )
        aliases = validation_aliases[1:]
        extra = getattr(info, "json_schema_extra", None) or {}
        if not isinstance(extra, Mapping):
            extra = {}
        annotation = getattr(info, "annotation", None)
        extra_keys = extra.get("extra_keys", ())
        if not isinstance(extra_keys, (list, tuple, set, frozenset)):
            extra_keys = ()
        for extra_key in extra_keys:
            if isinstance(extra_key, str) and extra_key not in aliases:
                aliases.append(extra_key)
        injected = bool(extra.get("injected") or extra.get("vault"))
        sensitive = bool(extra.get("sensitive") or injected) or _is_secret(annotation)
        is_required = getattr(info, "is_required", None)
        required = bool(is_required()) if callable(is_required) else False
        fields.append(
            EnvironmentField(
                field=str(name),
                env=str(canonical),
                aliases=tuple(alias for alias in aliases if alias != canonical),
                required=required,
                injected=injected,
                has_default=not required,
                sensitive=sensitive,
                group=str(extra.get("group", "Application")),
                description=str(getattr(info, "description", None) or ""),
            )
        )
    resolved_source = source or f"{model.__module__}.{model.__qualname__}"
    return EnvironmentManifest(source=resolved_source, fields=tuple(fields))


def validate_environment(
    manifest: EnvironmentManifest,
    environ: Mapping[str, str],
    *,
    require_injected: bool = False,
) -> EnvironmentValidation:
    """Resolve canonical names and aliases without exposing secret values."""

    missing: list[str] = []
    resolved: dict[str, str] = {}
    for field in manifest.fields:
        for candidate in (field.env, *field.aliases):
            value = environ.get(candidate)
            if isinstance(value, str) and value.strip():
                resolved[field.env] = candidate
                break
        else:
            if field.required or (require_injected and field.injected):
                missing.append(field.env)
    return EnvironmentValidation(tuple(sorted(missing)), resolved)


def _validation_aliases(alias: Any) -> tuple[str, ...]:
    if isinstance(alias, str):
        return (alias,)
    choices = getattr(alias, "choices", ())
    return tuple(str(choice) for choice in choices if isinstance(choice, str))


def _is_secret(annotation: Any) -> bool:
    return "secret" in getattr(annotation, "__name__", "").lower()


def _string(raw: Mapping[str, Any], key: str, *, required: bool = True) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key} is required")
    return value


def _boolean(raw: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be a boolean")
    return value
