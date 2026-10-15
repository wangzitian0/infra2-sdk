"""A standalone app's readiness boundary using the released SDK's public API."""

from __future__ import annotations

import asyncio
import json
import os

from infra2_sdk.runtime import (
    Dependency,
    DependencyCheck,
    DependencyKind,
    DependencyManifest,
    EnvironmentTier,
    ProbeResult,
    check_health,
    environment_from_env,
)
from infra2_sdk.runtime.http import probe_http

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


class CatalogCheck:
    """A DependencyCheck: a name and a probe() that returns a ProbeResult."""

    name = "catalog"

    def __init__(self, url: str) -> None:
        self.url = url

    async def probe(self) -> ProbeResult:
        return await probe_http(self.url, name=self.name)


async def main() -> int:
    try:
        environment = environment_from_env(required=True)
        url = os.environ.get("CATALOG_HEALTH_URL", "")
        checks: list[DependencyCheck] = [CatalogCheck(url)] if url else []
        status_code, report = await check_health(
            checks, manifest=DEPENDENCIES, tier=environment.tier, timeout_seconds=5
        )
    except ValueError as exc:
        print(json.dumps({"ready": False, "error": str(exc)}))
        return 1
    if status_code != 200:
        print(
            json.dumps({"ready": False, "status": report["status"], "reasons": report["reasons"]})
        )
        return 1
    print(json.dumps({"ready": True, "status": report["status"], "environment": environment.name}))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
