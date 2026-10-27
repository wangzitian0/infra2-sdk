import pytest

from infra2_sdk._wire import (
    parse_contract_version,
    parse_string,
    require_contract_version,
    validate_http_url,
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


def test_validate_http_url() -> None:
    validate_http_url("https://api.example.test/v1#section", label="endpoint")
    validate_http_url(
        "http://collector.internal:4318",
        label="OTLP endpoint",
        allow_fragment=False,
        validate_port=True,
    )
    with pytest.raises(ValueError, match="endpoint must use http:// or https:// with a host"):
        validate_http_url("ftp://example.test", label="endpoint")
    with pytest.raises(ValueError, match="endpoint must not contain credentials"):
        validate_http_url("https://user:pass@example.test", label="endpoint")
    with pytest.raises(ValueError, match="OTLP endpoint must not contain a fragment"):
        validate_http_url(
            "https://collector.internal:4318#frag",
            label="OTLP endpoint",
            allow_fragment=False,
        )
    with pytest.raises(ValueError, match="OTLP endpoint must be a valid HTTP URL"):
        validate_http_url(
            "http://collector.internal:badport",
            label="OTLP endpoint",
            validate_port=True,
        )


def test_canonical_sha256_is_key_order_independent_and_matches_the_runtime_helpers() -> None:
    import hashlib

    from infra2_sdk._wire import canonical_json_bytes, canonical_sha256

    left = {"b": [1, 2], "a": {"y": 1, "x": "é"}}
    right = {"a": {"x": "é", "y": 1}, "b": [1, 2]}
    assert canonical_json_bytes(left) == canonical_json_bytes(right)
    assert canonical_json_bytes(left) == b'{"a":{"x":"\\u00e9","y":1},"b":[1,2]}'
    assert canonical_sha256(left) == hashlib.sha256(canonical_json_bytes(left)).hexdigest()
    assert canonical_sha256({"a": 1}) != canonical_sha256({"a": 2})
