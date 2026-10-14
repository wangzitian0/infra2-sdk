"""One resolver answers tier, service name, version and instance id for every entry point.

``OtelSettings.from_env``, ``RuntimeIdentity.from_env`` and ``init_service`` read the same
facts from the environment. These tests pin the one precedence rule for the service name and
count the calls of the private resolver. A second copy of the resolution fails a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from infra2_sdk.runtime.identity import RuntimeIdentity
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


@pytest.mark.parametrize(
    "environ,expected",
    [
        pytest.param({**_ENV, **_ATTRIBUTE}, "from-env", id="env-beats-attribute"),
        pytest.param(_ENV, "from-env", id="env-alone"),
        pytest.param(_ATTRIBUTE, "from-attribute", id="attribute-alone"),
    ],
)
def test_runtime_identity_service_name_precedence(environ, expected) -> None:
    assert RuntimeIdentity.from_env(environ).service_name == expected


def test_runtime_identity_without_any_service_name_raises() -> None:
    with pytest.raises(ValueError, match="OTEL_SERVICE_NAME is required"):
        RuntimeIdentity.from_env({})


def test_runtime_identity_does_not_read_a_name_from_a_discarded_attribute() -> None:
    environ = {"OTEL_RESOURCE_ATTRIBUTES": "service.name=from-attribute,broken"}

    with (
        pytest.warns(RuntimeWarning, match="OTEL_RESOURCE_ATTRIBUTES"),
        pytest.raises(ValueError, match="OTEL_SERVICE_NAME is required"),
    ):
        RuntimeIdentity.from_env(environ)


def test_otel_settings_does_not_read_a_name_from_a_discarded_attribute() -> None:
    environ = {"OTEL_RESOURCE_ATTRIBUTES": "service.name=from-attribute,broken"}

    with pytest.warns(RuntimeWarning, match="OTEL_RESOURCE_ATTRIBUTES"):
        settings = OtelSettings.from_env(environ, default_service_name="from-default")

    assert settings.service_name == "from-default"


# --- version and instance id: the environment, then the resource attribute, then a default ------


def test_otel_settings_and_identity_agree_on_version_and_instance_id() -> None:
    attributes = "service.version=1.2.3,service.instance.id=pod-1"
    environ = {"OTEL_SERVICE_NAME": "api", "OTEL_RESOURCE_ATTRIBUTES": attributes}

    for source in (OtelSettings.from_env(environ), RuntimeIdentity.from_env(environ)):
        assert (source.service_version, source.instance_id) == ("1.2.3", "pod-1")

    overridden = {**environ, "SERVICE_VERSION": "9.9.9", "INSTANCE_ID": "pod-9"}
    for source in (OtelSettings.from_env(overridden), RuntimeIdentity.from_env(overridden)):
        assert (source.service_version, source.instance_id) == ("9.9.9", "pod-9")

    bare = {"OTEL_SERVICE_NAME": "api"}
    for source in (OtelSettings.from_env(bare), RuntimeIdentity.from_env(bare)):
        assert (source.service_version, source.instance_id) == ("unknown", "")


# --- a warning names the caller of from_env, not a line inside the SDK --------------------------


def test_resource_attribute_warning_names_the_caller_of_from_env() -> None:
    environ = {"OTEL_SERVICE_NAME": "api", "OTEL_RESOURCE_ATTRIBUTES": "broken"}

    with pytest.warns(RuntimeWarning, match="OTEL_RESOURCE_ATTRIBUTES") as from_otel:
        OtelSettings.from_env(environ)
    with pytest.warns(RuntimeWarning, match="OTEL_RESOURCE_ATTRIBUTES") as from_identity:
        RuntimeIdentity.from_env(environ)

    for caught in (from_otel, from_identity):
        assert [Path(warning.filename).name for warning in caught] == [Path(__file__).name]


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


def test_runtime_identity_from_env_calls_the_resolver_once(resolver_calls) -> None:
    RuntimeIdentity.from_env({"OTEL_SERVICE_NAME": "api"})

    assert len(resolver_calls) == 1
    assert resolver_calls[0]["strict"] is False


def test_runtime_identity_from_env_calls_the_resolver_once_in_strict_mode(resolver_calls) -> None:
    RuntimeIdentity.from_env({"ENVIRONMENT": "local_dev", "OTEL_SERVICE_NAME": "api"}, strict=True)

    assert len(resolver_calls) == 1
    assert resolver_calls[0]["strict"] is True
