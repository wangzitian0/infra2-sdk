"""CLI wrapper forwarding to infra2_sdk.scaffold."""

import sys

from infra2_sdk.scaffold import main

if __name__ == "__main__":
    sys.exit(main())
