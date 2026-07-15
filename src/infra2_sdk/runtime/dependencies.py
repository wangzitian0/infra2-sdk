"""Declarative external-dependency contract shared across runtime tiers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from infra2_sdk.runtime.environment import EnvironmentTier, resolve_environment_tier

DEPENDENCY_MANIFEST_VERSION = 1
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


class DependencyKind(StrEnum):
    CODE_DOMINANT = "code_dominant"
    MODEL_DOMINANT = "model_dominant"


@dataclass(frozen=True)
class Dependency:
    name: str
    kind: DependencyKind
    required_in: frozenset[EnvironmentTier]
    env_vars: frozenset[str]
    summary: str = ""
    local_backend: str = ""
    deployed_backend: str = ""

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ValueError("dependency name must be a non-empty identifier")
        if not self.required_in:
            raise ValueError(f"dependency {self.name!r} must be required in at least one tier")
        if not self.env_vars:
            raise ValueError(f"dependency {self.name!r} must declare environment variables")
        if any(not key or key.upper() != key for key in self.env_vars):
            raise ValueError("dependency environment variables must be uppercase")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "required_in": sorted(tier.value for tier in self.required_in),
            "env_vars": sorted(self.env_vars),
            "summary": self.summary,
            "local_backend": self.local_backend,
            "deployed_backend": self.deployed_backend,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Dependency:
        required = raw.get("required_in")
        env_vars = raw.get("env_vars")
        if not isinstance(required, list) or not isinstance(env_vars, list):
            raise ValueError("required_in and env_vars must be arrays")
        if any(not isinstance(value, str) for value in (*required, *env_vars)):
            raise ValueError("required_in and env_vars must contain strings")
        return cls(
            name=_string(raw, "name"),
            kind=DependencyKind(_string(raw, "kind")),
            required_in=frozenset(resolve_environment_tier(value) for value in required),
            env_vars=frozenset(env_vars),
            summary=_string(raw, "summary", required=False),
            local_backend=_string(raw, "local_backend", required=False),
            deployed_backend=_string(raw, "deployed_backend", required=False),
        )


class DependencyManifest:
    def __init__(
        self,
        dependencies: Iterable[Dependency],
        *,
        contract_version: int = DEPENDENCY_MANIFEST_VERSION,
    ) -> None:
        if contract_version != DEPENDENCY_MANIFEST_VERSION:
            raise ValueError(f"unsupported dependency manifest version {contract_version}")
        self.contract_version = contract_version
        values = tuple(dependencies)
        names = [dependency.name for dependency in values]
        if len(names) != len(set(names)):
            raise ValueError("duplicate dependency name")
        self._dependencies = values
        self._by_name = {dependency.name: dependency for dependency in values}

    def __iter__(self):
        return iter(self._dependencies)

    def __len__(self) -> int:
        return len(self._dependencies)

    def get(self, name: str) -> Dependency:
        return self._by_name[name]

    def names(self) -> frozenset[str]:
        return frozenset(self._by_name)

    def required_for(self, tier: str | EnvironmentTier) -> frozenset[str]:
        resolved = resolve_environment_tier(tier)
        return frozenset(
            dependency.name for dependency in self if resolved in dependency.required_in
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "dependencies": [dependency.to_dict() for dependency in self],
        }

    @staticmethod
    def json_schema() -> dict[str, Any]:
        return {
            "$schema": JSON_SCHEMA_DIALECT,
            "title": "Runtime dependency manifest",
            "type": "object",
            "additionalProperties": False,
            "required": ["contract_version", "dependencies"],
            "properties": {
                "contract_version": {"const": DEPENDENCY_MANIFEST_VERSION},
                "dependencies": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["name", "kind", "required_in", "env_vars"],
                        "properties": {
                            "name": {"type": "string", "minLength": 1},
                            "kind": {"enum": [kind.value for kind in DependencyKind]},
                            "required_in": {
                                "type": "array",
                                "items": {"enum": [tier.value for tier in EnvironmentTier]},
                                "minItems": 1,
                                "uniqueItems": True,
                            },
                            "env_vars": {
                                "type": "array",
                                "items": {"type": "string", "pattern": "^[A-Z][A-Z0-9_]*$"},
                                "minItems": 1,
                                "uniqueItems": True,
                            },
                            "summary": {"type": "string"},
                            "local_backend": {"type": "string"},
                            "deployed_backend": {"type": "string"},
                        },
                    },
                },
            },
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> DependencyManifest:
        try:
            contract_version = int(raw.get("contract_version", 0))
        except (TypeError, ValueError):
            raise ValueError("contract_version must be an integer") from None
        dependencies = raw.get("dependencies")
        if not isinstance(dependencies, list):
            raise ValueError("dependencies must be an array")
        if any(not isinstance(item, Mapping) for item in dependencies):
            raise ValueError("each dependency must be an object")
        return cls(
            (Dependency.from_dict(item) for item in dependencies),
            contract_version=contract_version,
        )


def _string(raw: Mapping[str, Any], key: str, *, required: bool = True) -> str:
    value = raw.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{key} is required")
    return value
