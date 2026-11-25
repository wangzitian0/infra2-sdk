"""Print the generated tables of docs/app-integration-contract.md.

Usage:
    python scripts/render_app_contract.py

The output replaces the text between the ``app-contract`` markers of the page.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Import the SDK of this checkout, never an installed copy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infra2_sdk.config import shared_env_contract  # noqa: E402
from infra2_sdk.delivery import CELL_POLICIES  # noqa: E402


def render() -> str:
    """Render shared environment names by group and app-owned delivery cells."""
    contract = shared_env_contract()
    lines = ["| Group | Canonical name | Sensitive |", "|---|---|---|"]
    for group, entries in contract["groups"].items():
        for entry in entries:
            sensitive = "yes" if entry.get("sensitive") else "no"
            lines.append(f"| {group} | `{entry['name']}` | {sensitive} |")

    lines += ["", "| Cell (app owned) | Trigger | Budget (s) | Check list |", "|---|---|---|---|"]
    for cell_id, policy in CELL_POLICIES.items():
        if policy.owner != "app":
            continue
        check_list = policy.check_list
        if isinstance(check_list, tuple):
            check_list = ", ".join(check_list)
        lines.append(
            f"| `{cell_id}` | {policy.trigger} | {policy.budget_seconds} | {check_list or '-'} |"
        )
    for cell_id, policy in CELL_POLICIES.items():
        if policy.owner == "app" and policy.trigger == "manual":
            lines += [
                "",
                f"`{cell_id}` is a manual cell for a person. No rule tells an agent to run it.",
            ]
    return "\n".join(lines) + "\n"


def main() -> int:
    sys.stdout.write(render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
