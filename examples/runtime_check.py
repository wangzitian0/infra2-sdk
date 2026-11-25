"""A standalone app's readiness boundary using the released SDK's public API."""

from __future__ import annotations

import asyncio
import json
import os

from infra2_sdk.config import EnvironmentTier, resolve_tier
from infra2_sdk.service import (
    Dependency,
    DependencyKind,
    DependencyManifest,
    ProbeResult,
    evaluate_health,
    probe_http,
)
from infra2_sdk.telemetry import OtelSettings

DEPENDENCIES = DependencyManifest(
    dependencies=(
        Dependency(
            name="catalog",
            kind=DependencyKind.CODE_DOMINANT,
            required_in=frozenset(EnvironmentTier),
            env_vars=frozenset({"CATALOG_HEALTH_URL"}),
        ),
    )
)


class CatalogCheck:
    """A readiness check: a name and an async probe() returning a ProbeResult."""

    name = "catalog"

    def __init__(self, url: str) -> None:
        self.url = url

    async def probe(self) -> ProbeResult:
        return await probe_http(self.url, name=self.name)


async def main() -> int:
    try:
        tier = resolve_tier(os.environ.get("ENVIRONMENT", ""))
        settings = OtelSettings.from_env(strict=True)
        url = os.environ.get("CATALOG_HEALTH_URL", "")
        checks: list[CatalogCheck] = [CatalogCheck(url)] if url else []
        status_code, report = await evaluate_health(
            checks, manifest=DEPENDENCIES, tier=tier, timeout_seconds=5
        )
    except ValueError as exc:
        print(json.dumps({"ready": False, "error": str(exc)}))
        return 1
    if status_code != 200:
        print(
            json.dumps({"ready": False, "status": report["status"], "reasons": report["reasons"]})
        )
        return 1
    print(
        json.dumps(
            {
                "ready": True,
                "status": report["status"],
                "service_name": settings.service_name,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
