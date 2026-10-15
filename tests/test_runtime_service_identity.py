"""One resolver answers tier, service name, version and instance id for every entry point.

``OtelSettings.from_env`` and ``init_service`` read the same facts from the environment. These
tests pin the one precedence rule for the service name and count the calls of the private
resolver. A second copy of the resolution fails a test.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from infra2_sdk.runtime._otel_env import load_resource_attributes
from infra2_sdk.runtime.otel import OtelSettings

_ENV = {"OTEL_SERVICE_NAME": "from-env"}
_ATTRIBUTE = {"OTEL_RESOURCE_ATTRIBUTES": "service.name=from-attribute"}


# --- service name: OTEL_SERVICE_NAME, then service.name, then the default, then unknown_service


@pytest.mark.parametrize(
    "environ,default,expected",
    [
        pytest.param({**_ENV, **_ATTRIBUTE}, "from-default", "from-env", id="env-beats-all"),
        pytest.param(_ENV, "from-default", "from-env", id="env-beats-default"),
        pytest.param(_ENV, None, "from-env", id="env-alone"),
        pytest.param(_ATTRIBUTE, "from-default", "from-attribute", id="attribute-beats-default"),
        pytest.param(_ATTRIBUTE, None, "from-attribute", id="attribute-alone"),
        pytest.param({}, "from-default", "from-default", id="default-alone"),
        pytest.param({}, None, "unknown_service", id="nothing-gives-unknown_service"),
    ],
)
def test_otel_settings_service_name_precedence(environ, default, expected) -> None:
    options = {} if default is None else {"default_service_name": default}

    assert OtelSettings.from_env(environ, **options).service_name == expected


def test_otel_settings_does_not_read_a_name_from_a_discarded_attribute() -> None:
    environ = {"OTEL_RESOURCE_ATTRIBUTES": "service.name=from-attribute,broken"}

    with pytest.warns(RuntimeWarning, match="OTEL_RESOURCE_ATTRIBUTES"):
        settings = OtelSettings.from_env(environ, default_service_name="from-default")

    assert settings.service_name == "from-default"


# --- version and instance id: the environment, then the resource attribute, then a default ------


def test_otel_settings_resolves_version_and_instance_id() -> None:
    attributes = "service.version=1.2.3,service.instance.id=pod-1"
    environ = {"OTEL_SERVICE_NAME": "api", "OTEL_RESOURCE_ATTRIBUTES": attributes}

    source = OtelSettings.from_env(environ)
    assert (source.service_version, source.instance_id) == ("1.2.3", "pod-1")

    overridden = {**environ, "SERVICE_VERSION": "9.9.9", "INSTANCE_ID": "pod-9"}
    source = OtelSettings.from_env(overridden)
    assert (source.service_version, source.instance_id) == ("9.9.9", "pod-9")

    bare = {"OTEL_SERVICE_NAME": "api"}
    source = OtelSettings.from_env(bare)
    assert (source.service_version, source.instance_id) == ("unknown", "")


# --- a blank or padded attribute is missing or stripped, like an environment variable ----------

# attribute -> (field of the settings, the value when the attribute is missing)
_IDENTITY_ATTRIBUTES = {
    "service.name": ("service_name", "unknown_service"),
    "service.version": ("service_version", "unknown"),
    "service.instance.id": ("instance_id", ""),
}
_BLANK_OR_PADDED = [
    pytest.param("%20", None, id="space"),
    pytest.param("%09", None, id="tab"),
    pytest.param("%20x%20", "x", id="padded"),
]


@pytest.mark.parametrize("raw,stripped", _BLANK_OR_PADDED)
@pytest.mark.parametrize("attribute", list(_IDENTITY_ATTRIBUTES))
def test_otel_settings_reads_a_blank_or_padded_attribute_as_missing_or_stripped(
    attribute, raw, stripped
) -> None:
    field, missing = _IDENTITY_ATTRIBUTES[attribute]

    settings = OtelSettings.from_env({"OTEL_RESOURCE_ATTRIBUTES": f"{attribute}={raw}"})

    assert getattr(settings, field) == (missing if stripped is None else stripped)
    if stripped is None:
        assert attribute not in settings.resource_attributes
    else:
        assert settings.resource_attributes[attribute] == stripped


@pytest.mark.parametrize("raw", ["%20", "%09"])
def test_a_blank_service_name_attribute_lets_the_default_name_apply(raw) -> None:
    environ = {"OTEL_RESOURCE_ATTRIBUTES": f"service.name={raw}"}

    settings = OtelSettings.from_env(environ, default_service_name="from-default")

    assert settings.service_name == "from-default"


# --- a warning names the caller of from_env, not a line inside the SDK --------------------------


def test_resource_attribute_warning_names_the_caller_of_from_env() -> None:
    environ = {"OTEL_SERVICE_NAME": "api", "OTEL_RESOURCE_ATTRIBUTES": "broken"}

    with pytest.warns(RuntimeWarning, match="OTEL_RESOURCE_ATTRIBUTES") as from_otel:
        OtelSettings.from_env(environ)

    assert [Path(warning.filename).name for warning in from_otel] == [Path(__file__).name]


def test_load_resource_attributes_has_no_default_stack_level() -> None:
    parameter = inspect.signature(load_resource_attributes).parameters["stacklevel"]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty, "each caller counts its own frames"


def test_load_resource_attributes_gives_the_stack_level_to_warn() -> None:
    with pytest.warns(RuntimeWarning, match="OTEL_RESOURCE_ATTRIBUTES") as level_one:
        load_resource_attributes("broken", strict=False, stacklevel=1)
    with pytest.warns(RuntimeWarning, match="OTEL_RESOURCE_ATTRIBUTES") as level_two:
        load_resource_attributes("broken", strict=False, stacklevel=2)

    assert [Path(w.filename).name for w in level_one] == ["_otel_env.py"]
    assert [Path(w.filename).name for w in level_two] == [Path(__file__).name]


# --- one resolver: a second copy of the resolution changes a count -------------------------------


def test_otel_settings_from_env_calls_the_resolver_once(resolver_calls) -> None:
    OtelSettings.from_env({"OTEL_SERVICE_NAME": "api"}, default_service_name="fallback")

    assert len(resolver_calls) == 1
    assert resolver_calls[0]["strict"] is False
    assert resolver_calls[0]["default_service_name"] == "fallback"


def test_otel_settings_from_env_calls_the_resolver_once_in_strict_mode(resolver_calls) -> None:
    OtelSettings.from_env({"ENVIRONMENT": "local_dev", "OTEL_SERVICE_NAME": "api"}, strict=True)

    assert len(resolver_calls) == 1
    assert resolver_calls[0]["strict"] is True
