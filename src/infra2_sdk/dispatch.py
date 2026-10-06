"""Backwards-compatible shim for deploy dispatch.

All capabilities have been consolidated into :mod:`infra2_sdk.deploy`.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from infra2_sdk.deploy import (
    INFRA_REPOSITORY,
    RECEIVER_EVENT_TYPE,
    RECEIVER_WORKFLOW_FILE,
    Api,
    LogFetcher,
    ReceiverRun,
    dispatch_and_wait,
    dispatch_main,
    github_api_client,
)

__all__ = [
    "INFRA_REPOSITORY",
    "RECEIVER_EVENT_TYPE",
    "RECEIVER_WORKFLOW_FILE",
    "Api",
    "LogFetcher",
    "ReceiverRun",
    "dispatch_and_wait",
    "github_api_client",
    "main",
]


def main(argv: Sequence[str] | None = None) -> int:
    mod = sys.modules[__name__]
    return dispatch_main(
        argv,
        api_client_factory=getattr(mod, "github_api_client", github_api_client),
        dispatch_fn=getattr(mod, "dispatch_and_wait", dispatch_and_wait),
    )


if __name__ == "__main__":
    raise SystemExit(main())
