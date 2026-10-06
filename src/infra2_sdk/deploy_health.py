"""Backwards-compatible shim for deployment health polling.

All capabilities have been consolidated into :mod:`infra2_sdk.deploy`.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from infra2_sdk.deploy import (
    HealthCheckResult,
    HttpGet,
    default_http_get,
    deploy_health_main,
    poll_until_healthy,
)

__all__ = [
    "HealthCheckResult",
    "HttpGet",
    "default_http_get",
    "main",
    "poll_until_healthy",
]


def main(argv: Sequence[str] | None = None) -> int:
    mod = sys.modules[__name__]
    return deploy_health_main(
        argv,
        http_get_factory=getattr(mod, "default_http_get", default_http_get),
        poll_fn=getattr(mod, "poll_until_healthy", poll_until_healthy),
    )


if __name__ == "__main__":
    raise SystemExit(main())
