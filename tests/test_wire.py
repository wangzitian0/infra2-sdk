import pytest

from infra2_sdk._wire import (
    _integer,
    _string,
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

    # Alias check
    assert _string({"a": "b"}, "a") == "b"


def test_parse_integer() -> None:
    assert parse_integer({"count": 42}, "count") == 42
    assert parse_integer({}, "count", required=False, default=10) == 10

    with pytest.raises(ValueError, match="count is required"):
        parse_integer({}, "count", required=True)
    with pytest.raises(ValueError, match="count must be an integer"):
        parse_integer({"count": "42"}, "count")
    with pytest.raises(ValueError, match="count must be an integer"):
        parse_integer({"count": True}, "count")

    # Alias check
    assert _integer({"a": 1}, "a") == 1
