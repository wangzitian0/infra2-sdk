import ast
from pathlib import Path

import pytest

from infra2_sdk import _wire
from infra2_sdk._wire import (
    parse_contract_version,
    parse_integer,
    parse_string,
    require_contract_version,
)


def test_require_contract_version() -> None:
    assert require_contract_version(1, 1, description="test") == 1
    with pytest.raises(ValueError, match="contract_version must be an integer"):
        require_contract_version("1", 1, description="test")
    with pytest.raises(ValueError, match="unsupported test 2"):
        require_contract_version(2, 1, description="test")


def test_parse_contract_version() -> None:
    assert parse_contract_version({"contract_version": 2}, 2, description="v2") == 2
    with pytest.raises(ValueError, match="unsupported v2 0"):
        parse_contract_version({}, 2, description="v2")


def test_parse_string() -> None:
    assert parse_string({"key": "value"}, "key") == "value"
    assert parse_string({"key": "  spaced  "}, "key") == "spaced"
    assert parse_string({"key": ""}, "key", required=False) == ""

    with pytest.raises(ValueError, match="key is required"):
        parse_string({}, "key", required=True)
    with pytest.raises(ValueError, match="key is required"):
        parse_string({"key": "   "}, "key", required=True)
    with pytest.raises(ValueError, match="key must be a string"):
        parse_string({"key": 123}, "key")


def test_parse_integer() -> None:
    assert parse_integer({"count": 42}, "count") == 42
    assert parse_integer({}, "count", required=False, default=10) == 10

    with pytest.raises(ValueError, match="count is required"):
        parse_integer({}, "count", required=True)
    with pytest.raises(ValueError, match="count must be an integer"):
        parse_integer({"count": "42"}, "count")
    with pytest.raises(ValueError, match="count must be an integer"):
        parse_integer({"count": True}, "count")


_SRC = Path(__file__).resolve().parents[1] / "src" / "infra2_sdk"
_DIALECT_URL = "https://json-schema.org/draft/2020-12/schema"


def test_json_schema_dialect_has_one_definition_in_the_source_tree() -> None:
    literal_sites = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value == _DIALECT_URL:
                literal_sites.append(path.relative_to(_SRC).as_posix())
    assert literal_sites == ["_wire.py"]
    assert _wire.JSON_SCHEMA_DIALECT == _DIALECT_URL


def test_json_schema_dialect_is_the_same_value_in_every_schema_emitter() -> None:
    from infra2_sdk import snapshot
    from infra2_sdk.runtime import config_schema, health, identity

    for module in (config_schema, health, identity, snapshot):
        assert module.JSON_SCHEMA_DIALECT is _wire.JSON_SCHEMA_DIALECT, module.__name__
    schema = snapshot.AnonymizedSnapshotManifest.json_schema()
    assert schema["$schema"] == _wire.JSON_SCHEMA_DIALECT
