"""Guards for the environment variable names in the source of the SDK (issue #102).

The tests read the syntax trees of src/infra2_sdk. They do not import the modules.

- No string constant equals the name of a variable that 3.4.0 removed.
- The fixed names that the source reads are the names of the runtime env contract plus the list
  FIXED_READS below. A new fixed-name read fails until a person adds it there with a reason.

A read site is a call of get, getenv, pop, setdefault, resolve_runtime_env, resolve_env, env_int
or runtime_env_spec, a subscript, or a test of the form `"NAME" in mapping`, with a string
constant that looks like a variable name. The receiver can have any name.

These tests do not check:

- a name built at run time, for example a prefix plus a suffix;
- a name passed in a variable, for example environ.get(NAME). The first test still finds the
  constant that holds NAME when its value is a removed name;
- the tests directory and the README.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# The frozen list lives in wheel_smoke.py. Do not derive it from the source tree.
from wheel_smoke import REMOVED_ENV_NAMES

import infra2_sdk
from infra2_sdk.runtime.environment import runtime_env_contract

SRC = Path(infra2_sdk.__file__).resolve().parent

# A string constant that equals a removed name: (path below src/infra2_sdk, name) -> reason.
ALLOWED_LITERALS = {
    ("scaffold.py", "DATABASE_URL"): (
        "the manifest of the generated application names the variable that the generated "
        "application reads; the SDK does not read it"
    ),
}

# Fixed variable names that the source reads and that are not in the runtime env contract.
FIXED_READS = {
    "GITHUB_ACTIONS": "environment_from_env: the mapping that the caller passes",
    "VAULT_ADDR": "VaultKvBackend.from_environ: the mapping that the caller passes",
    "VAULT_TOKEN": "VaultKvBackend.from_environ: the mapping that the caller passes",
    "VAULT_ROLE_ID": "VaultKvBackend.from_environ: the mapping that the caller passes",
    "VAULT_SECRET_ID": "VaultKvBackend.from_environ: the mapping that the caller passes",
}

_VARIABLE_NAME = re.compile(r"[A-Z][A-Z0-9_]{3,}")
_FIRST_ARGUMENT = frozenset({"get", "getenv", "pop", "setdefault", "runtime_env_spec"})
_SECOND_ARGUMENT = frozenset({"resolve_runtime_env", "resolve_env", "env_int"})


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _constants(node: ast.AST | None) -> list[str]:
    """String constants of a node, or of the elements of a tuple or a list."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Tuple | ast.List):
        return [value for element in node.elts for value in _constants(element)]
    return []


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    return None


def _read_sites(tree: ast.AST) -> list[tuple[int, str]]:
    """(line, name) for each read site whose name looks like a variable name."""
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Subscript):
            names = _constants(node.slice)
        elif isinstance(node, ast.Compare) and any(
            isinstance(op, ast.In | ast.NotIn) for op in node.ops
        ):
            names = _constants(node.left)
        elif isinstance(node, ast.Call):
            callee = _callee(node)
            if callee in _FIRST_ARGUMENT:
                names = _constants(node.args[0]) if node.args else []
            elif callee in _SECOND_ARGUMENT:
                names = _constants(node.args[1]) if len(node.args) > 1 else []
                for keyword in node.keywords:
                    if keyword.arg in {"key", "aliases"}:
                        names += _constants(keyword.value)
        sites += [(node.lineno, name) for name in names if _VARIABLE_NAME.fullmatch(name)]
    return sites


def _string_constants(tree: ast.AST) -> list[tuple[int, str]]:
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_the_scan_reads_every_source_file() -> None:
    files = _source_files()
    assert len(files) > 20, "the scan found almost no file: the next tests would check nothing"
    for path in files:
        _parse(path)


def test_no_string_constant_equals_a_removed_variable_name() -> None:
    removed = set(REMOVED_ENV_NAMES)
    offenders = []
    for path in _source_files():
        relative = path.relative_to(SRC).as_posix()
        for line, value in _string_constants(_parse(path)):
            if value in removed and (relative, value) not in ALLOWED_LITERALS:
                offenders.append(f"{relative}:{line}: {value}")
    assert not offenders, f"the source mentions a removed variable name: {offenders}"


def test_every_allowed_literal_is_still_needed() -> None:
    assert ALLOWED_LITERALS, "the allow-list is empty: this test would check nothing"
    for (relative, name), reason in ALLOWED_LITERALS.items():
        assert name in REMOVED_ENV_NAMES, f"{name} is not a removed name"
        values = {value for _, value in _string_constants(_parse(SRC / relative))}
        assert name in values, f"{relative} no longer holds {name}: remove the entry"
        assert reason.strip()


def test_fixed_name_reads_are_the_contract_plus_the_allow_list() -> None:
    found = {name for path in _source_files() for _, name in _read_sites(_parse(path))}
    variables = runtime_env_contract()["variables"]
    registered = {item["name"] for item in variables} | {
        alias for item in variables for alias in item["aliases"]
    }
    unexpected = sorted(found - registered - set(FIXED_READS))
    missing = sorted(set(FIXED_READS) - found)
    assert not unexpected, f"the source reads a fixed name that nothing records: {unexpected}"
    assert not missing, f"FIXED_READS lists a name that no read site uses any more: {missing}"


def test_the_allow_list_does_not_hide_a_removed_or_a_contract_name() -> None:
    variables = runtime_env_contract()["variables"]
    registered = {item["name"] for item in variables} | {
        alias for item in variables for alias in item["aliases"]
    }
    assert FIXED_READS, "the allow-list is empty: this test would check nothing"
    assert not set(FIXED_READS) & set(REMOVED_ENV_NAMES)
    assert not set(FIXED_READS) & registered
    assert all(reason.strip() for reason in FIXED_READS.values())


SYNTHETIC = """
import os
a = os.environ.get("AAAA_ONE")
b = os.environ["BBBB_TWO"]
c = os.getenv("CCCC_THREE")
d = values.get("DDDD_FOUR", "")
e = resolve_runtime_env(env, "EEEE_FIVE")
f = resolve_env(env, "FFFF_SIX", aliases=("GGGG_SEVEN", "HHHH_EIGHT"))
g = env_int(env, key="IIII_NINE", default=1)
h = runtime_env_spec("JJJJ_TEN")
i = "KKKK_ELEVEN" in os.environ
j = os.environ.pop("LLLL_TWELVE", None)
k = os.environ.setdefault("MMMM_THIRTEEN", "1")
l = "NNNN_FOURTEEN" not in values
not_a_name = other.get("lower_case")
too_short = other.get("ABC")
from_variable = environ.get(NAME)
second_argument_of_get = other.get("kkkk", "LLLL_DEFAULT")
"""


def test_the_read_site_scan_finds_each_kind_of_read_in_a_synthetic_source() -> None:
    found = {name for _, name in _read_sites(ast.parse(SYNTHETIC))}
    assert found == {
        "AAAA_ONE",
        "BBBB_TWO",
        "CCCC_THREE",
        "DDDD_FOUR",
        "EEEE_FIVE",
        "FFFF_SIX",
        "GGGG_SEVEN",
        "HHHH_EIGHT",
        "IIII_NINE",
        "JJJJ_TEN",
        "KKKK_ELEVEN",
        "LLLL_TWELVE",
        "MMMM_THIRTEEN",
        "NNNN_FOURTEEN",
    }


@pytest.mark.parametrize("name", ["DATABASE_URL", "SERVICE_NAME", "S3_BUCKET"])
def test_the_literal_scan_finds_a_removed_name_in_every_kind_of_position(name: str) -> None:
    for code in (
        f'x = "{name}"',
        f'os.environ.get("{name}")',
        f'values["{name}"]',
        f'call(aliases=("{name}",))',
        f'f"{name}"',
    ):
        assert name in {value for _, value in _string_constants(ast.parse(code))}, code
