"""Print the README environment table from ``runtime_env_contract()``.

Usage:
    python scripts/render_env_contract.py

The output is the Markdown table between the ``env-contract`` markers of README.md.
``tests/test_contract_documentation.py`` compares the two byte for byte.
This script is a development tool. The wheel does not ship it.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Import the SDK of this checkout, never an installed copy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infra2_sdk.runtime.environment import runtime_env_contract  # noqa: E402

SUPPORTED_CONTRACT_VERSION = 1
HEADER = "| Canonical name | Compatibility aliases | Sensitive |"
SEPARATOR = "|---|---|---|"


def render(contract: Mapping[str, Any]) -> str:
    """Return the table, one row per variable in contract order, ending with a newline."""
    if contract.get("contract_version") != SUPPORTED_CONTRACT_VERSION:
        raise ValueError(f"this renderer supports contract version {SUPPORTED_CONTRACT_VERSION}")
    lines = [HEADER, SEPARATOR]
    for variable in contract["variables"]:
        aliases = ", ".join(f"`{alias}`" for alias in variable["aliases"]) or "none"
        sensitive = "yes" if variable["sensitive"] else "no"
        lines.append(f"| `{variable['name']}` | {aliases} | {sensitive} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    # Write bytes: the output must not depend on the newline rules of the platform.
    sys.stdout.buffer.write(render(runtime_env_contract()).encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
