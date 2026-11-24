"""Print the generated tables of docs/app-integration-contract.md.

Usage:
    python scripts/render_app_contract.py

The output replaces the text between the ``app-contract`` markers of the page.
``tests/test_app_integration_contract.py`` compares the two byte for byte.
This script is a development tool. The wheel does not ship it.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Import the SDK of this checkout, never an installed copy.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from infra2_sdk.manifests import CELL_POLICIES  # noqa: E402
from infra2_sdk.runtime.environment import shared_env_contract  # noqa: E402


def render() -> str:
    """The shared environment names by group, the cells that an app owns, and its manual cells."""
    contract = shared_env_contract()
    lines = ["| Group | Canonical name | Sensitive | Owners |", "|---|---|---|---|"]
    for group, entries in contract["groups"].items():
        for entry in entries:
            owners = ", ".join(entry.get("owners") or ()) or "none"
            sensitive = "yes" if entry.get("sensitive") else "no"
            lines.append(f"| {group} | `{entry['name']}` | {sensitive} | {owners} |")
    lines += ["", "| Deprecated alias | Use |", "|---|---|"]
    for entries in contract["groups"].values():
        for entry in entries:
            for alias in entry.get("deprecated_aliases") or ():
                lines.append(f"| `{alias}` | `{entry['name']}` |")
    lines += ["", "| Retired name | Use |", "|---|---|"]
    for entry in contract.get("retired") or ():
        lines.append(f"| `{entry['name']}` | `{entry['use']}` |")
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
