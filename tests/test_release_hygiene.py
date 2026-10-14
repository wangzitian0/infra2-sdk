"""Guards for the release surface a human has to keep in step with ``pyproject.toml``.

These fail on the repository as it is today if any part drifts: they compare against what the
code defines (the package version, the modules on disk), not against a copy of the answer.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "infra2_sdk"
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
VERSION = PYPROJECT["project"]["version"]
README = (ROOT / "README.md").read_text("utf-8")

SEMVER = r"\d+\.\d+\.\d+"


def test_readme_install_pins_are_the_current_release() -> None:
    git_pins = re.findall(rf"infra2-sdk\.git@v({SEMVER})", README)
    wheel_pins = re.findall(rf"releases/download/v({SEMVER})/infra2_sdk-({SEMVER})-py3", README)
    assert git_pins, "README no longer shows a git install pin: this guard is checking nothing"
    assert wheel_pins, "README no longer shows a wheel URL: this guard is checking nothing"
    assert set(git_pins) == {VERSION}
    assert {version for pair in wheel_pins for version in pair} == {VERSION}


def test_changelog_has_an_entry_for_the_package_version() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text("utf-8")
    headings = re.findall(rf"^## ({SEMVER}) - \d{{4}}-\d{{2}}-\d{{2}}$", changelog, re.M)
    assert headings, "CHANGELOG has no '## X.Y.Z - YYYY-MM-DD' entries"
    assert headings[0] == VERSION, f"newest CHANGELOG entry is {headings[0]}, package is {VERSION}"
    assert len(headings) == len(set(headings))


def _public_modules() -> list[str]:
    modules = []
    for path in sorted(SRC.rglob("*.py")):
        parts = path.relative_to(SRC.parent).with_suffix("").parts
        if any(part.startswith("_") for part in parts):
            continue  # private modules, package __init__ and __main__
        modules.append(".".join(parts))
    return modules


def test_every_public_module_is_in_the_readme_module_table() -> None:
    modules = _public_modules()
    assert "infra2_sdk.runtime.health" in modules and "infra2_sdk.routing" in modules
    missing = [name for name in modules if f"| `{name}` |" not in README]
    assert not missing, f"README module table is missing: {missing}"


def test_readme_module_table_has_no_phantom_rows() -> None:
    rows = re.findall(r"^\| `(infra2_sdk[\w.]*)` \|", README, re.M)
    assert "infra2_sdk.runtime.health" in rows, "README module table not found: guard is empty"
    assert len(rows) == len(set(rows)), "README module table has a duplicate row"
    phantom = [name for name in rows if name not in _public_modules()]
    assert not phantom, f"README module table lists modules that do not exist: {phantom}"


def test_py_typed_marker_ships_with_the_package() -> None:
    assert (SRC / "py.typed").is_file()
    package_data = PYPROJECT["tool"]["setuptools"]["package-data"]["infra2_sdk"]
    assert "py.typed" in package_data
    assert "data/*.yaml" in package_data


def test_an_empty_parametrization_stops_the_run_instead_of_skipping(tmp_path: Path) -> None:
    ini = PYPROJECT["tool"]["pytest"]["ini_options"]
    assert ini.get("empty_parameter_set_mark") == "fail_at_collect"
    case = tmp_path / "test_empty_case.py"
    case.write_text(
        "import pytest\n\n\n@pytest.mark.parametrize('x', [])\ndef test_it(x):\n    pass\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-c",
            str(ROOT / "pyproject.toml"),
            "--rootdir",
            str(tmp_path),
            str(case),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    report = result.stdout + result.stderr
    assert result.returncode == 2, report
    assert "Empty parameter set in 'test_it'" in report, report
    assert "skipped" not in report, report
