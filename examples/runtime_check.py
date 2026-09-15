"""A standalone app's readiness boundary using the released SDK's public API."""

from __future__ import annotations

import asyncio
import json
import os

from infra2_sdk.runtime import (
    Dependency,
    DependencyKind,
    DependencyManifest,
    DependencyUnavailableError,
    EnvironmentTier,
    RuntimeIdentity,
    assert_required_dependencies,
    environment_from_env,
    run_probes,
)
from infra2_sdk.runtime.http import HttpCheck

# This is application policy: name the dependency and the tiers that require it.
# Keep this declaration in your app; the SDK owns validation and probe execution.
DEPENDENCIES = DependencyManifest(
    [
        Dependency(
            name="catalog",
            kind=DependencyKind.CODE_DOMINANT,
            required_in=frozenset(EnvironmentTier),
            env_vars=frozenset({"CATALOG_HEALTH_URL"}),
        )
    ]
)


async def main() -> int:
    try:
        environment = environment_from_env(required=True)
        identity = RuntimeIdentity.from_env(strict=True)
        url = os.environ.get("CATALOG_HEALTH_URL", "")
        checks = [HttpCheck("catalog", url)] if url else []
        results = await run_probes(checks, timeout_seconds=5)
        assert_required_dependencies(DEPENDENCIES, environment.tier, results)
    except (ValueError, DependencyUnavailableError) as exc:
        print(json.dumps({"ready": False, "error": str(exc)}))
        return 1
    print(json.dumps({"ready": True, "identity": identity.to_dict()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
