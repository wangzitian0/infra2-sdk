"""Guards for the environment variable names in the source of the SDK (issue #102).

The tests read the syntax trees of src/infra2_sdk. They do not import the modules.

- No string constant equals the name of a variable that 3.4.0 removed. ALLOWED_LITERALS names the
  few places where such a constant is text and not a read: the file, the value and the place.
- The names at the read sites of the source are exactly the values of RuntimeEnvKey plus the list
  FIXED_READS below. A new fixed-name read fails until a person adds it there with a reason.
  A member of RuntimeEnvKey that no read site names also fails: the contract lists a variable
  that no code reads.
- No read site names an alias of the contract (ENV, APP_ENV, IMAGE_TAG). An alias is read through
  resolve_runtime_env only.

A read site is one of these forms. The receiver can have any name.

- A call of get, getenv, pop, setdefault, resolve_runtime_env, resolve_env, env_int or
  runtime_env_spec, with the name as the first or second positional argument or as the keyword
  `key`. `getattr(mapping, "get")("NAME")` counts as a call of get.
- A subscript: `mapping["NAME"]`.
- A test of the form `"NAME" in mapping`.
- A comparison `k == "NAME"` in a for loop or in a comprehension over `mapping.items()`.

A name is a str or bytes constant, or a RuntimeEnvKey.MEMBER, that matches `[A-Z][A-Z0-9_]{2,}`.

These tests do not check:

- a name built at run time, for example a prefix plus a suffix;
- a name passed in a variable, for example environ.get(NAME). The first test still finds the
  constant that holds NAME when its value is a removed name. A refactor that passes a
  RuntimeEnvKey member in a variable makes the second test report the member as unread;
- a read that uses the whole mapping: a loop over `os.environ` keys with `startswith`, a copy
  of `os.environ`, a lookup through a lazy `__getattr__`;
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
from infra2_sdk.runtime.environment import RuntimeEnvKey, runtime_env_contract

SRC = Path(infra2_sdk.__file__).resolve().parent

# A string constant that equals a removed name and is text, not a read:
# (path below src/infra2_sdk, value, place) -> reason. The place is the call and the keyword that
# hold the constant, for example "EnvironmentField(env=)". The same value in another place of the
# same file is not allowed. An empty dict is the state to aim for.
ALLOWED_LITERALS = {
    ("scaffold.py", "DATABASE_URL", "EnvironmentField(env=)"): (
        "generated text, not a read: the manifest field of the generated application names the "
        "variable that the generated application reads; the SDK does not read it"
    ),
}

# Fixed variable names that the source reads and that are not in the runtime env contract.
FIXED_READS = {
    "GITHUB_ACTIONS": "environment_from_env: the mapping that the caller passes",
    "OTEL_METRIC_EXPORT_INTERVAL": "OtelSettings.from_env: OpenTelemetry metric export interval",
    "OTEL_SDK_DISABLED": "OtelSettings.from_env: OpenTelemetry SDK disable switch",
    "OTEL_TRACES_SAMPLER": "OtelSettings.from_env: OpenTelemetry trace sampler name",
    "OTEL_TRACES_SAMPLER_ARG": "OtelSettings.from_env: OpenTelemetry trace sampler argument",
    "VAULT_ADDR": "VaultKvBackend.from_environ: the mapping that the caller passes",
    "VAULT_TOKEN": "VaultKvBackend.from_environ: the mapping that the caller passes",
    "VAULT_ROLE_ID": "VaultKvBackend.from_environ: the mapping that the caller passes",
    "VAULT_SECRET_ID": "VaultKvBackend.from_environ: the mapping that the caller passes",
}

_VARIABLE_NAME = re.compile(r"[A-Z][A-Z0-9_]{2,}")
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_FIRST_ARGUMENT = frozenset({"get", "getenv", "pop", "setdefault", "runtime_env_spec"})
_SECOND_ARGUMENT = frozenset({"resolve_runtime_env", "resolve_env", "env_int"})


def _source_files() -> list[Path]:
    return sorted(SRC.rglob("*.py"))


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _names_in(node: ast.AST | None) -> list[str]:
    """Variable names of a node: constants, RuntimeEnvKey.MEMBER, elements of a tuple or list."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
        return [node.value.decode("ascii", "replace")]
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "RuntimeEnvKey"
        and node.attr in RuntimeEnvKey.__members__
    ):
        return [RuntimeEnvKey[node.attr].value]
    if isinstance(node, ast.Tuple | ast.List):
        return [value for element in node.elts for value in _names_in(element)]
    return []


def _callee(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    if isinstance(call.func, ast.Name):
        return call.func.id
    if (
        isinstance(call.func, ast.Call)
        and _callee(call.func) == "getattr"
        and len(call.func.args) > 1
    ):
        attribute = call.func.args[1]  # getattr(mapping, "get")("NAME")
        if isinstance(attribute, ast.Constant) and isinstance(attribute.value, str):
            return attribute.value
    return None


def _iterates_over_items(node: ast.AST) -> bool:
    """True for a for loop or a comprehension over `something.items()`."""
    if isinstance(node, ast.For):
        iterables = [node.iter]
    elif isinstance(node, _COMPREHENSIONS):
        iterables = [generator.iter for generator in node.generators]
    else:
        return False
    return any(
        isinstance(iterable, ast.Call)
        and isinstance(iterable.func, ast.Attribute)
        and iterable.func.attr == "items"
        for iterable in iterables
    )


def _read_sites(tree: ast.AST) -> list[tuple[int, str]]:
    """(line, name) for each read site whose name looks like a variable name."""
    sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        names: list[str] = []
        if isinstance(node, ast.Subscript):
            names = _names_in(node.slice)
        elif isinstance(node, ast.Compare) and any(
            isinstance(op, ast.In | ast.NotIn) for op in node.ops
        ):
            names = _names_in(node.left)
        elif isinstance(node, ast.Call):
            callee = _callee(node)
            if callee in _FIRST_ARGUMENT:
                names = _names_in(node.args[0]) if node.args else []
                names += [n for k in node.keywords if k.arg == "key" for n in _names_in(k.value)]
            elif callee in _SECOND_ARGUMENT:
                names = _names_in(node.args[1]) if len(node.args) > 1 else []
                for keyword in node.keywords:
                    if keyword.arg in {"key", "aliases"}:
                        names += _names_in(keyword.value)
        sites += [(node.lineno, name) for name in names if _VARIABLE_NAME.fullmatch(name)]
        if _iterates_over_items(node):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Compare) and any(
                    isinstance(op, ast.Eq | ast.NotEq) for op in inner.ops
                ):
                    sides = (inner.left, *inner.comparators)
                    sites += [
                        (inner.lineno, name)
                        for side in sides
                        for name in _names_in(side)
                        if _VARIABLE_NAME.fullmatch(name)
                    ]
    return sorted(set(sites))


def _string_constants(tree: ast.AST) -> list[tuple[int, str, str]]:
    """(line, value, place) for each str or bytes constant.

    The place is "Call(keyword=)" when a keyword argument holds the constant, else "other".
    """
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Constant) and isinstance(node.value, str | bytes)):
            continue
        value = node.value if isinstance(node.value, str) else node.value.decode("ascii", "replace")
        keyword = parents.get(node)
        call = parents.get(keyword)
        place = "other"
        if isinstance(keyword, ast.keyword) and isinstance(call, ast.Call):
            place = f"{_callee(call)}({keyword.arg}=)"
        found.append((node.lineno, value, place))
    return found


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
        for line, value, place in _string_constants(_parse(path)):
            if value in removed and (relative, value, place) not in ALLOWED_LITERALS:
                offenders.append(f"{relative}:{line}: {value} in {place}")
    assert not offenders, f"the source mentions a removed variable name: {offenders}"


def _unused_literal_entries(
    allowed: dict[tuple[str, str, str], str], constants: dict[str, set[tuple[str, str]]]
) -> list[str]:
    """Problems of the entries of the allow-list. An entry that no constant matches is stale."""
    problems = []
    for (relative, value, place), reason in allowed.items():
        if value not in REMOVED_ENV_NAMES:
            problems.append(f"{value} is not a removed name")
        if (value, place) not in constants.get(relative, set()):
            problems.append(f"{relative} no longer holds {value} in {place}: remove the entry")
        if not reason.strip():
            problems.append(f"{relative} {value}: the entry has no reason")
    return problems


def test_every_allowed_literal_is_still_needed() -> None:
    # An empty allow-list is the state to aim for. Then the check has no entry to read.
    constants = {
        path.relative_to(SRC).as_posix(): {(v, p) for _, v, p in _string_constants(_parse(path))}
        for path in _source_files()
    }
    assert _unused_literal_entries(ALLOWED_LITERALS, constants) == []


def test_the_stale_literal_check_reports_each_kind_of_problem() -> None:
    constants = {"a.py": {("DATABASE_URL", "EnvironmentField(env=)")}}
    allowed = {
        ("a.py", "DATABASE_URL", "EnvironmentField(env=)"): "generated text",
        ("a.py", "DATABASE_URL", "other"): "the place is gone",
        ("b.py", "DATABASE_URL", "other"): "the file is gone",
        ("a.py", "ENVIRONMENT", "other"): "not a removed name",
        ("a.py", "S3_BUCKET", "EnvironmentField(env=)"): "  ",
    }
    problems = _unused_literal_entries(allowed, constants)
    assert problems == [
        "a.py no longer holds DATABASE_URL in other: remove the entry",
        "b.py no longer holds DATABASE_URL in other: remove the entry",
        "ENVIRONMENT is not a removed name",
        "a.py no longer holds ENVIRONMENT in other: remove the entry",
        "a.py no longer holds S3_BUCKET in EnvironmentField(env=): remove the entry",
        "a.py S3_BUCKET: the entry has no reason",
    ]
    assert _unused_literal_entries({}, constants) == []


def _read_name_differences(
    found: set[str], contract_keys: set[str], allowed: set[str]
) -> tuple[list[str], list[str]]:
    """(names read but not expected, names expected but not read)."""
    expected = contract_keys | allowed
    return sorted(found - expected), sorted(expected - found)


def test_the_names_at_read_sites_are_the_contract_keys_plus_the_allow_list() -> None:
    found = {name for path in _source_files() for _, name in _read_sites(_parse(path))}
    contract_keys = {key.value for key in RuntimeEnvKey}
    unexpected, missing = _read_name_differences(found, contract_keys, set(FIXED_READS))
    assert not unexpected, f"the source reads a fixed name that nothing records: {unexpected}"
    assert not missing, f"no read site names these variables of the contract or the list: {missing}"


def test_the_comparison_of_read_names_reports_both_directions() -> None:
    contract_keys = {"A_NAME", "B_NAME"}
    assert _read_name_differences({"A_NAME", "B_NAME", "C_NAME"}, contract_keys, {"C_NAME"}) == (
        [],
        [],
    )
    assert _read_name_differences({"A_NAME", "B_NAME", "X_NAME"}, contract_keys, set()) == (
        ["X_NAME"],
        [],
    )
    assert _read_name_differences({"A_NAME"}, contract_keys, {"C_NAME"}) == (
        [],
        ["B_NAME", "C_NAME"],
    )


def test_no_read_site_names_an_alias_of_the_contract() -> None:
    variables = runtime_env_contract()["variables"]
    aliases = {alias for item in variables for alias in item["aliases"]}
    found = {name for path in _source_files() for _, name in _read_sites(_parse(path))}
    named = sorted(found & aliases)
    assert not named, f"a read site names an alias; use resolve_runtime_env instead: {named}"


def test_the_allow_list_does_not_hide_a_removed_or_a_contract_name() -> None:
    variables = runtime_env_contract()["variables"]
    registered = {item["name"] for item in variables} | {
        alias for item in variables for alias in item["aliases"]
    }
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
m = resolve_runtime_env(env, RuntimeEnvKey.SERVICE_NAME)
n = env_int(env, key=RuntimeEnvKey.INSTANCE_ID, default=1)
o = resolve_runtime_env(env, Other.SERVICE_NAME)
p = resolve_runtime_env(env, RuntimeEnvKey.NO_SUCH_MEMBER)
not_a_name = other.get("lower_case")
too_short = other.get("AB")
q = os.environ.get(key="OOOO_KEYWORD")
r = getattr(os.environ, "get")("PPPP_GETATTR")
s = os.environb.get(b"QQQQ_BYTES")
t = [v for k, v in os.environ.items() if k == "RRRR_ITEMS"]
u = os.environ.get("ENV")
v = {k: 1 for k, _ in environ.items() if "SSSS_COMPREHENSION" != k}
for k, v in environ.items():
    if k == "TTTT_LOOP":
        pass
other_loop = [x for x in other.keys() if x == "UUUU_NOT_ITEMS"]
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
        "OTEL_SERVICE_NAME",
        "INSTANCE_ID",
        "OOOO_KEYWORD",
        "PPPP_GETATTR",
        "QQQQ_BYTES",
        "RRRR_ITEMS",
        "ENV",
        "SSSS_COMPREHENSION",
        "TTTT_LOOP",
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
        assert name in {value for _, value, _ in _string_constants(ast.parse(code))}, code
