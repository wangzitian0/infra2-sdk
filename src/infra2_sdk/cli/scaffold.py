"""CLI wrapper forwarding to infra2_sdk.scaffold."""

from __future__ import annotations

import sys

from infra2_sdk.scaffold import main

if __name__ == "__main__":
    sys.exit(main())
