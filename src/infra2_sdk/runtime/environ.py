"""Backwards-compatible shim for runtime environment variables.

All capabilities have been consolidated into :mod:`infra2_sdk.runtime.environment`.
"""

from __future__ import annotations

from infra2_sdk.runtime.environment import (
    RUNTIME_ENV_CONTRACT_VERSION,
    RUNTIME_ENV_SPECS,
    EnvironmentConflictError,
    ResolvedEnvValue,
    RuntimeEnvKey,
    RuntimeEnvSpec,
    env_bool,
    env_float,
    env_int,
    resolve_env,
    resolve_runtime_env,
    runtime_env_contract,
    runtime_env_spec,
)

__all__ = [
    "RUNTIME_ENV_CONTRACT_VERSION",
    "RUNTIME_ENV_SPECS",
    "EnvironmentConflictError",
    "ResolvedEnvValue",
    "RuntimeEnvKey",
    "RuntimeEnvSpec",
    "env_bool",
    "env_float",
    "env_int",
    "resolve_env",
    "resolve_runtime_env",
    "runtime_env_contract",
    "runtime_env_spec",
]
