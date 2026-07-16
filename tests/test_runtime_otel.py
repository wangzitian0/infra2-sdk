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


def test_otel_settings_load_standard_env_and_preserve_preview_display_name() -> None:
    settings = OtelSettings.from_env(
        {
            "ENVIRONMENT": "pr-42",
            "OTEL_SERVICE_NAME": "api",
            "SERVICE_VERSION": "a1b2c3d",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            "OTEL_RESOURCE_ATTRIBUTES": (
                "deployment.environment=pr-42,team.name=finance%20platform"
            ),
            "OTEL_METRIC_EXPORT_INTERVAL": "30000",
        }
    )
    assert settings.environment == "preview"
    assert settings.deployment_environment == "pr-42"
    assert settings.resource_attributes["team.name"] == "finance platform"
    assert settings.export_interval_millis == 30_000
    assert resource_attributes(settings)["deployment.environment.name"] == "pr-42"


def test_otel_is_transparently_disabled_without_endpoint_or_by_standard_flag() -> None:
    no_endpoint = OtelSettings.from_env({})
    assert no_endpoint.enabled is False
    assert no_endpoint.service_name == "unknown_service"
    disabled = OtelSettings.from_env(
        {
            "OTEL_SERVICE_NAME": "api",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            "OTEL_SDK_DISABLED": "true",
        }
    )
    assert disabled.enabled is False


def test_otel_uses_standard_resource_attributes_without_custom_sdk_variables() -> None:
    settings = OtelSettings.from_env(
        {
            "OTEL_RESOURCE_ATTRIBUTES": (
                "service.name=portable-api,service.version=1.2.3,"
                "deployment.environment.name=staging"
            )
        }
    )
    assert settings.service_name == "portable-api"
    assert settings.service_version == "1.2.3"
    assert settings.environment == "staging"
    assert settings.deployment_environment == "staging"


def test_otel_accepts_provider_neutral_preview_display_names() -> None:
    settings = OtelSettings.from_env(
        {
            "ENVIRONMENT": "preview",
            "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=review-slot-202",
        }
    )
    assert settings.environment == "preview"
    assert settings.deployment_environment == "review-slot-202"


def test_otel_strict_mode_and_boolean_grammar_fail_closed() -> None:
    with pytest.raises(ValueError, match="ENVIRONMENT is required"):
        OtelSettings.from_env({}, strict=True)
    with pytest.raises(ValueError, match="true or false"):
        OtelSettings.from_env(
            {"ENVIRONMENT": "local_dev", "OTEL_SDK_DISABLED": "1"},
            strict=True,
        )


def test_otel_non_strict_mode_reports_and_discards_invalid_standard_values() -> None:
    with pytest.warns(RuntimeWarning, match="true or false"):
        settings = OtelSettings.from_env({"OTEL_SDK_DISABLED": "1"})
    assert settings.enabled is False
    with pytest.warns(RuntimeWarning, match="key=value"):
        settings = OtelSettings.from_env({"OTEL_RESOURCE_ATTRIBUTES": "invalid"})
    assert settings.resource_attributes == {}


def test_otel_rejects_malformed_or_mismatched_resource_environment() -> None:
    with pytest.raises(ValueError, match="OTEL_RESOURCE_ATTRIBUTES"):
        OtelSettings.from_env(
            {
                "ENVIRONMENT": "local_dev",
                "OTEL_SERVICE_NAME": "api",
                "OTEL_RESOURCE_ATTRIBUTES": "missing-equals",
            },
            strict=True,
        )
    with pytest.raises(ValueError, match="disagrees"):
        OtelSettings.from_env(
            {
                "ENVIRONMENT": "staging",
                "OTEL_SERVICE_NAME": "api",
                "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment=production",
            },
            strict=True,
        )


def test_otel_resource_attributes_decode_keys_and_reject_bad_percent_escapes() -> None:
    settings = OtelSettings.from_env({"OTEL_RESOURCE_ATTRIBUTES": "service%2Ename=portable-api"})
    assert settings.service_name == "portable-api"
    with pytest.raises(ValueError, match="percent escape"):
        OtelSettings.from_env(
            {
                "ENVIRONMENT": "local_dev",
                "OTEL_RESOURCE_ATTRIBUTES": "service.name=bad%ZZ",
            },
            strict=True,
        )
    with pytest.raises(ValueError, match="UTF-8"):
        OtelSettings.from_env(
            {
                "ENVIRONMENT": "local_dev",
                "OTEL_RESOURCE_ATTRIBUTES": "service.name=%FF",
            },
            strict=True,
        )


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
