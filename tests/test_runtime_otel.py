import pytest

from infra2_sdk.runtime.environment import EnvironmentTier
from infra2_sdk.runtime.identity import RuntimeIdentity
from infra2_sdk.runtime.otel import (
    OtelSettings,
    configure_telemetry,
    inject_trace_context,
    resource_attributes,
)


def test_disabled_bootstrap_has_no_global_or_background_side_effects() -> None:
    settings = OtelSettings(service_name="api", enabled=False)
    providers = configure_telemetry(settings)
    assert providers.tracer_provider is None
    providers.shutdown()
    with pytest.raises(ValueError, match="unknown environment"):
        OtelSettings(service_name="api", environment="typo", enabled=False)


def test_identity_populates_standard_resource_attributes() -> None:
    identity = RuntimeIdentity(
        service_name="api",
        service_version="1.2.3",
        environment=EnvironmentTier.STAGING,
        commit_sha="a" * 40,
        instance_id="pod-1",
    )
    settings = OtelSettings.from_identity(
        identity,
        endpoint="http://collector:4318",
        resource_attributes={"team.name": "finance"},
    )
    attributes = resource_attributes(settings)
    assert attributes["service.name"] == "api"
    assert attributes["service.instance.id"] == "pod-1"
    assert attributes["vcs.ref.head.revision"] == "a" * 40
    assert attributes["team.name"] == "finance"
    assert attributes["service.name"] == "api"


def test_w3c_trace_context_injection_preserves_headers() -> None:
    assert inject_trace_context({"X-Test": "1"})["X-Test"] == "1"


def test_enabled_bootstrap_constructs_all_otlp_signal_providers() -> None:
    providers = configure_telemetry(
        OtelSettings(
            service_name="sdk-canary",
            endpoint="http://127.0.0.1:4318/v1/traces",
            export_interval_millis=3_600_000,
        )
    )
    assert type(providers.tracer_provider).__name__ == "TracerProvider"
    assert type(providers.meter_provider).__name__ == "MeterProvider"
    assert type(providers.logger_provider).__name__ == "LoggerProvider"
    assert providers.logging_instrumentor is None
    providers.shutdown()


@pytest.mark.parametrize(
    "changes,message",
    [
        ({"service_name": ""}, "service_name"),
        ({"endpoint": None}, "endpoint"),
        ({"endpoint": "grpc://collector"}, "http"),
        ({"export_interval_millis": 0}, "positive"),
        ({"resource_attributes": {"bad": 1}}, "resource attributes"),
    ],
)
def test_otel_settings_validation(changes, message) -> None:
    values = {"service_name": "api", "endpoint": "http://collector:4318"} | changes
    with pytest.raises(ValueError, match=message):
        OtelSettings(**values)
