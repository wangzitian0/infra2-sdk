"""Print the README environment table from ``shared_env_contract()``.

Usage:
    python scripts/render_env_contract.py

This script is a development tool. The wheel does not ship it.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Import the SDK of this checkout, never an installed copy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infra2_sdk.config import shared_env_contract  # noqa: E402

HEADER = "| Group | Canonical name | Sensitive |"
SEPARATOR = "|---|---|---|"


def render(contract: Mapping[str, Any]) -> str:
    """Return the canonical shared environment variable table ending with a newline."""
    lines = [HEADER, SEPARATOR]
    for group, entries in contract["groups"].items():
        for entry in entries:
            sensitive = "yes" if entry.get("sensitive") else "no"
            lines.append(f"| {group} | `{entry['name']}` | {sensitive} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    sys.stdout.buffer.write(render(shared_env_contract()).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
