import logging
import warnings
from inspect import Parameter, signature

import pytest

from infra2_sdk.runtime import otel as otel_module
from infra2_sdk.runtime._otel_env import parse_traces_sampler
from infra2_sdk.runtime.environment import EnvironmentTier
from infra2_sdk.runtime.identity import RuntimeIdentity
from infra2_sdk.runtime.otel import (
    OtelSettings,
    configure_telemetry,
    extract_trace_context,
    inject_trace_context,
    resource_attributes,
    signal_endpoint,
)


def test_disabled_bootstrap_has_no_global_or_background_side_effects() -> None:
    settings = OtelSettings(service_name="api", enabled=False)
    providers = configure_telemetry(settings)
    assert providers.tracer_provider is None
    providers.shutdown()
    with pytest.raises(ValueError, match="unknown environment"):
        OtelSettings(service_name="api", environment="typo", enabled=False)


def test_deployment_environment_does_not_rebind_existing_otel_arguments() -> None:
    settings = OtelSettings(
        "api",
        "http://collector:4318",
        "1.2.3",
        "staging",
        "pod-1",
        {"team.name": "finance"},
        False,
        30_000,
        deployment_environment="staging",
    )
    assert settings.instance_id == "pod-1"
    assert settings.resource_attributes == {"team.name": "finance"}
    assert (
        signature(OtelSettings).parameters["deployment_environment"].kind is Parameter.KEYWORD_ONLY
    )


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
    assert disabled.endpoint is None


def test_disabled_flag_takes_precedence_over_invalid_endpoint() -> None:
    settings = OtelSettings.from_env(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "grpc://ignored.invalid",
            "OTEL_SDK_DISABLED": "true",
        }
    )
    assert settings.enabled is False
    assert settings.endpoint is None


def test_otel_reads_standard_instance_id() -> None:
    settings = OtelSettings.from_env({"INSTANCE_ID": "worker-17"})
    assert settings.instance_id == "worker-17"
    assert resource_attributes(settings)["service.instance.id"] == "worker-17"


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
    with pytest.warns(RuntimeWarning, match="OTEL_SDK_DISABLED"):
        settings = OtelSettings.from_env({"OTEL_SDK_DISABLED": "1"})
    assert settings.enabled is False
    with pytest.warns(RuntimeWarning, match="OTEL_RESOURCE_ATTRIBUTES"):
        settings = OtelSettings.from_env({"OTEL_RESOURCE_ATTRIBUTES": "invalid"})
    assert settings.resource_attributes == {}


def test_otel_non_strict_discards_conflicting_resource_environment_aliases() -> None:
    with pytest.warns(RuntimeWarning, match="OTEL_RESOURCE_ATTRIBUTES"):
        settings = OtelSettings.from_env(
            {
                "OTEL_RESOURCE_ATTRIBUTES": (
                    "deployment.environment.name=staging,deployment.environment=production"
                )
            }
        )
    assert settings.environment == "local_dev"
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
    assert providers.logging_handler is None
    providers.shutdown()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://",
        "https://:4318",
        "http://bad host",
        "http://user:password@collector:4318",
        "http://collector:4318/path#fragment",
        "http://collector:not-a-port",
        "http://collector:99999",
    ],
)
def test_otel_rejects_unsafe_or_incomplete_endpoints(endpoint: str) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        OtelSettings(service_name="api", endpoint=endpoint)


def test_signal_endpoint_preserves_query_after_appending_signal_path() -> None:
    assert signal_endpoint("https://collector.example.test/base?tenant=alpha", "traces") == (
        "https://collector.example.test/base/v1/traces?tenant=alpha"
    )


@pytest.mark.parametrize(
    "base",
    [
        "http://collector:4318",
        "http://collector:4318/",
        "http://collector:4318/v1/traces",
        "http://collector:4318/v1/logs/",
    ],
)
@pytest.mark.parametrize("signal", ["traces", "metrics", "logs"])
def test_signal_endpoint_derives_each_signal_from_any_base_form(base: str, signal: str) -> None:
    assert signal_endpoint(base, signal) == f"http://collector:4318/v1/{signal}"


def test_signal_endpoint_requires_a_base() -> None:
    with pytest.raises(ValueError, match="endpoint is required"):
        signal_endpoint(None, "traces")


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


# --- sampler ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,arg,expected",
    [
        (None, None, ("parentbased_always_on", None)),
        ("", "0.5", ("parentbased_always_on", None)),
        ("always_on", "ignored", ("always_on", None)),
        (" ALWAYS_OFF ", None, ("always_off", None)),
        ("parentbased_always_off", None, ("parentbased_always_off", None)),
        ("traceidratio", None, ("traceidratio", 1.0)),
        ("traceidratio", "0", ("traceidratio", 0.0)),
        ("parentbased_traceidratio", " 0.25 ", ("parentbased_traceidratio", 0.25)),
    ],
)
def test_traces_sampler_parsing_follows_the_otel_specification(name, arg, expected) -> None:
    assert parse_traces_sampler(name, arg) == expected


@pytest.mark.parametrize(
    "name,arg,message",
    [
        ("jaeger_remote", None, "unsupported OTEL_TRACES_SAMPLER"),
        ("sometimes", "0.5", "unsupported OTEL_TRACES_SAMPLER"),
        ("traceidratio", "lots", "between 0 and 1"),
        ("traceidratio", "1.5", "between 0 and 1"),
        ("parentbased_traceidratio", "-0.1", "between 0 and 1"),
        ("traceidratio", "nan", "between 0 and 1"),
    ],
)
def test_traces_sampler_parsing_rejects_unknown_names_and_bad_ratios(name, arg, message) -> None:
    with pytest.raises(ValueError, match=message):
        parse_traces_sampler(name, arg)


def test_sampler_settings_load_from_standard_env_and_normalize() -> None:
    settings = OtelSettings.from_env(
        {
            "OTEL_SERVICE_NAME": "api",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            "OTEL_TRACES_SAMPLER": "ParentBased_TraceIdRatio",
            "OTEL_TRACES_SAMPLER_ARG": " 0.1 ",
        }
    )
    assert (settings.traces_sampler, settings.traces_sampler_arg) == (
        "parentbased_traceidratio",
        "0.1",
    )
    unset = OtelSettings.from_env({"OTEL_SERVICE_NAME": "api"})
    assert (unset.traces_sampler, unset.traces_sampler_arg) == ("", "")
    keyword_only = signature(OtelSettings).parameters
    assert keyword_only["traces_sampler"].kind is Parameter.KEYWORD_ONLY
    assert keyword_only["traces_sampler_arg"].kind is Parameter.KEYWORD_ONLY


def test_invalid_sampler_is_fatal_when_strict_and_reported_then_discarded_otherwise() -> None:
    environ = {
        "ENVIRONMENT": "local_dev",
        "OTEL_SERVICE_NAME": "api",
        "OTEL_TRACES_SAMPLER": "traceidratio",
        "OTEL_TRACES_SAMPLER_ARG": "2",
    }
    with pytest.raises(ValueError, match="OTEL_TRACES_SAMPLER_ARG"):
        OtelSettings.from_env(environ, strict=True)
    with pytest.warns(RuntimeWarning, match="OTEL_TRACES_SAMPLER"):
        settings = OtelSettings.from_env(environ)
    assert (settings.traces_sampler, settings.traces_sampler_arg) == ("", "")
    with pytest.raises(ValueError, match="unsupported OTEL_TRACES_SAMPLER"):
        OtelSettings(service_name="api", endpoint="http://collector:4318", traces_sampler="nope")


# --- in-memory harness: no network, no ambient global state ----------------------------------


class _MemoryOtlp:
    """Replaces the OTLP/HTTP exporters with in-memory ones and records their construction."""

    def __init__(self) -> None:
        from opentelemetry.sdk._logs import export as log_export
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        # Newer SDKs renamed the in-memory log exporter and deprecate the old name.
        memory_logs = getattr(log_export, "InMemoryLogRecordExporter", None)
        self.spans = InMemorySpanExporter()
        self.logs = (memory_logs or log_export.InMemoryLogExporter)()
        self.endpoints: list[tuple[str, str]] = []

    def metric_exporter(self, endpoint: str):
        from opentelemetry.sdk.metrics.export import MetricExporter, MetricExportResult

        class Memory(MetricExporter):
            def export(self, metrics_data, timeout_millis=10_000, **kwargs):
                return MetricExportResult.SUCCESS

            def force_flush(self, timeout_millis=10_000):
                return True

            def shutdown(self, timeout_millis=30_000, **kwargs):
                return None

        self.endpoints.append(("metrics", endpoint))
        return Memory()


@pytest.fixture
def memory_otlp(monkeypatch):
    import opentelemetry.exporter.otlp.proto.http._log_exporter as log_exporter
    import opentelemetry.exporter.otlp.proto.http.metric_exporter as metric_exporter
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as trace_exporter

    memory = _MemoryOtlp()

    def span_exporter(*, endpoint):
        memory.endpoints.append(("traces", endpoint))
        return memory.spans

    def log_exporter_factory(*, endpoint):
        memory.endpoints.append(("logs", endpoint))
        return memory.logs

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", span_exporter)
    monkeypatch.setattr(log_exporter, "OTLPLogExporter", log_exporter_factory)
    monkeypatch.setattr(
        metric_exporter, "OTLPMetricExporter", lambda *, endpoint: memory.metric_exporter(endpoint)
    )
    return memory


@pytest.fixture
def clean_otel_globals():
    """OpenTelemetry providers are set-once per process: reset them around a test."""
    from opentelemetry import trace
    from opentelemetry._logs import _internal as logs_internal
    from opentelemetry.metrics import _internal as metrics_internal
    from opentelemetry.util._once import Once

    targets = (
        (trace, "_TRACER_PROVIDER_SET_ONCE", "_TRACER_PROVIDER"),
        (metrics_internal, "_METER_PROVIDER_SET_ONCE", "_METER_PROVIDER"),
        (logs_internal, "_LOGGER_PROVIDER_SET_ONCE", "_LOGGER_PROVIDER"),
    )
    saved = [
        (module, once, getattr(module, once), provider, getattr(module, provider))
        for module, once, provider in targets
    ]
    root = logging.getLogger()
    handlers_before = list(root.handlers)

    def reset() -> None:
        for module, once, _, provider, _ in saved:
            setattr(module, once, Once())
            setattr(module, provider, None)
        otel_module._ACTIVE_GLOBAL = None

    reset()
    yield
    from opentelemetry.instrumentation.logging import LoggingInstrumentor

    instrumentor = LoggingInstrumentor()
    if instrumentor.is_instrumented_by_opentelemetry:  # a failed test must not leak it
        instrumentor.uninstrument()
    for handler in list(root.handlers):
        if handler not in handlers_before:
            root.removeHandler(handler)
    for module, once, once_value, provider, provider_value in saved:
        setattr(module, once, once_value)
        setattr(module, provider, provider_value)
    otel_module._ACTIVE_GLOBAL = None


def _settings(**changes) -> OtelSettings:
    return OtelSettings(
        service_name="sdk-test",
        endpoint="http://collector:4318",
        export_interval_millis=3_600_000,
        **changes,
    )


def _sampled_span_count(settings: OtelSettings, memory: _MemoryOtlp, *, context=None) -> int:
    providers = configure_telemetry(settings)
    try:
        tracer = providers.tracer_provider.get_tracer("sampler-test")
        with tracer.start_as_current_span("op", context=context):
            pass
        providers.tracer_provider.force_flush()
        return len(memory.spans.get_finished_spans())
    finally:
        providers.shutdown()


def test_configure_telemetry_wires_each_signal_to_its_own_otlp_path(memory_otlp) -> None:
    providers = configure_telemetry(_settings())
    providers.shutdown()
    assert sorted(memory_otlp.endpoints) == [
        ("logs", "http://collector:4318/v1/logs"),
        ("metrics", "http://collector:4318/v1/metrics"),
        ("traces", "http://collector:4318/v1/traces"),
    ]


@pytest.mark.parametrize(
    "sampler,arg,sampled",
    [
        ("", "", True),
        ("always_on", "", True),
        ("always_off", "", False),
        ("parentbased_always_on", "", True),
        ("parentbased_always_off", "", False),
        ("traceidratio", "0", False),
        ("traceidratio", "1", True),
        ("parentbased_traceidratio", "0", False),
        ("parentbased_traceidratio", "1", True),
    ],
)
def test_configured_sampler_decides_whether_a_root_span_is_exported(
    memory_otlp, sampler, arg, sampled
) -> None:
    settings = _settings(traces_sampler=sampler, traces_sampler_arg=arg)
    assert _sampled_span_count(settings, memory_otlp) == (1 if sampled else 0)


def test_ratio_sampler_threshold_uses_the_configured_probability(memory_otlp) -> None:
    providers = configure_telemetry(
        _settings(traces_sampler="traceidratio", traces_sampler_arg="0.5")
    )
    try:
        sampler = providers.tracer_provider.sampler
        # TraceIdRatioBased keeps trace ids whose low 64 bits fall below ratio * 2**64.
        assert sampler.should_sample(None, 1, "n").decision.is_sampled()
        assert sampler.should_sample(None, 2**63 - 1, "n").decision.is_sampled()
        assert not sampler.should_sample(None, 2**63, "n").decision.is_sampled()
    finally:
        providers.shutdown()


SAMPLED_PARENT = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
UNSAMPLED_PARENT = {"traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00"}


@pytest.mark.parametrize(
    "sampler,parent,sampled",
    [
        ("parentbased_always_off", SAMPLED_PARENT, True),
        ("parentbased_always_on", UNSAMPLED_PARENT, False),
        ("always_off", SAMPLED_PARENT, False),
        ("always_on", UNSAMPLED_PARENT, True),
        ("parentbased_traceidratio", UNSAMPLED_PARENT, False),
    ],
)
def test_parent_based_samplers_follow_the_extracted_inbound_decision(
    memory_otlp, sampler, parent, sampled
) -> None:
    settings = _settings(traces_sampler=sampler, traces_sampler_arg="1")
    context = extract_trace_context(parent)
    assert _sampled_span_count(settings, memory_otlp, context=context) == (1 if sampled else 0)


# --- W3C trace context: extract / inject -----------------------------------------------------


@pytest.fixture
def tracer():
    """A recording tracer on a private provider: no global state, no exporter."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    return TracerProvider(sampler=ALWAYS_ON).get_tracer("trace-context-test")


def test_inject_writes_a_valid_traceparent_for_the_active_span(tracer) -> None:
    with tracer.start_as_current_span("outbound") as span:
        headers = inject_trace_context({"X-Test": "1"})
        context = span.get_span_context()
    assert headers["X-Test"] == "1"
    # Newer SDKs also set the W3C "random trace id" flag, so compare the full flags byte.
    flags = f"{int(context.trace_flags):02x}"
    assert headers["traceparent"] == f"00-{context.trace_id:032x}-{context.span_id:016x}-{flags}"
    assert int(flags, 16) & 0x01, "the sampled bit must be set for a sampled span"


def test_inject_writes_nothing_without_an_active_span_and_does_not_mutate_the_input() -> None:
    original = {"X-Test": "1"}
    assert inject_trace_context(original) == {"X-Test": "1"}
    assert original == {"X-Test": "1"}
    assert "traceparent" not in inject_trace_context()


def test_extract_then_inject_keeps_the_trace_id_under_a_new_span_id(tracer) -> None:
    inbound = {"Traceparent": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
    context = extract_trace_context(inbound)
    with tracer.start_as_current_span("server", context=context) as span:
        outbound = inject_trace_context({})
        assert span.get_span_context().trace_id == int("0af7651916cd43dd8448eb211c80319c", 16)
    version, trace_id, span_id, flags = outbound["traceparent"].split("-")
    assert (version, trace_id) == ("00", "0af7651916cd43dd8448eb211c80319c")
    assert int(flags, 16) & 0x01, "the inbound sampled decision must be propagated"
    assert span_id == f"{span.get_span_context().span_id:016x}"
    assert span_id != "b7ad6b7169203331"


def test_extract_matches_header_names_case_insensitively(tracer) -> None:
    inbound = {"TRACEPARENT": "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"}
    with tracer.start_as_current_span("server", context=extract_trace_context(inbound)) as span:
        assert span.get_span_context().trace_id == int("0af7651916cd43dd8448eb211c80319c", 16)


def test_extract_ignores_non_string_header_values_instead_of_raising(tracer) -> None:
    # Raw ASGI/WSGI carriers can hold bytes; the W3C propagator would raise TypeError on them.
    inbound = {
        "traceparent": b"00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01",
        "tracestate": 5,
        b"x-bytes-name": "ignored",
    }
    with tracer.start_as_current_span("server", context=extract_trace_context(inbound)) as span:
        assert span.parent is None


def test_inject_with_an_explicit_context_passes_the_inbound_parent_through() -> None:
    context = extract_trace_context(
        {**SAMPLED_PARENT, "tracestate": "vendor=opaque"},
    )
    carried = inject_trace_context({}, context=context)
    assert carried["traceparent"] == SAMPLED_PARENT["traceparent"]
    assert carried["tracestate"] == "vendor=opaque"


@pytest.mark.parametrize(
    "inbound",
    [
        None,
        {},
        {"traceparent": "garbage"},
        {"traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-01"},
    ],
)
def test_missing_or_malformed_inbound_context_starts_a_new_root_trace(tracer, inbound) -> None:
    with tracer.start_as_current_span("server", context=extract_trace_context(inbound)) as span:
        assert span.parent is None
        assert span.get_span_context().is_valid


# --- set_global=True: providers, handler, idempotence ----------------------------------------


def _otel_handlers():
    from opentelemetry.sdk._logs import LoggingHandler as SdkHandler

    handler_class = otel_module._logging_handler_class()
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, (handler_class, SdkHandler))
    ]


class _Collect(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_set_global_installs_providers_and_exports_correlated_logs(
    memory_otlp, clean_otel_globals
) -> None:
    from opentelemetry import _logs, metrics, trace

    providers = configure_telemetry(_settings(), set_global=True)
    collected = _Collect()
    app_logger = logging.getLogger("infra2.sdk.test.global")
    app_logger.setLevel(logging.INFO)
    app_logger.addHandler(collected)
    try:
        assert trace.get_tracer_provider() is providers.tracer_provider
        assert metrics.get_meter_provider() is providers.meter_provider
        assert _logs.get_logger_provider() is providers.logger_provider
        assert _otel_handlers() == [providers.logging_handler]
        assert providers.logging_instrumentor.is_instrumented_by_opentelemetry

        with trace.get_tracer("app").start_as_current_span("request") as span:
            app_logger.warning("handled request")
        context = span.get_span_context()
        providers.logger_provider.force_flush()

        exported = memory_otlp.logs.get_finished_logs()
        assert [log.log_record.body for log in exported] == ["handled request"]
        assert exported[0].log_record.trace_id == context.trace_id
        assert exported[0].log_record.span_id == context.span_id
        resource = getattr(exported[0], "resource", None) or exported[0].log_record.resource
        assert resource.attributes["service.name"] == "sdk-test"
        assert collected.records[0].otelTraceID == f"{context.trace_id:032x}"
    finally:
        app_logger.removeHandler(collected)
        providers.shutdown()

    assert _otel_handlers() == []
    assert providers.logging_handler is None
    after = _Collect()
    app_logger.addHandler(after)
    try:
        app_logger.warning("after shutdown")
    finally:
        app_logger.removeHandler(after)
    assert not hasattr(after.records[0], "otelTraceID")


def test_set_global_twice_returns_the_active_installation_without_a_second_handler(
    memory_otlp, clean_otel_globals
) -> None:
    first = configure_telemetry(_settings(), set_global=True)
    exporters_built = len(memory_otlp.endpoints)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            second = configure_telemetry(_settings(), set_global=True)
        assert second is first
        assert len(memory_otlp.endpoints) == exporters_built == 3
        assert _otel_handlers() == [first.logging_handler]

        with pytest.warns(RuntimeWarning, match="already configured globally"):
            changed = configure_telemetry(_settings(traces_sampler="always_off"), set_global=True)
        assert changed is first
        assert len(memory_otlp.endpoints) == 3
    finally:
        first.shutdown()
    assert _otel_handlers() == []


def test_set_global_after_shutdown_builds_new_providers_and_reports_the_set_once_limit(
    memory_otlp, clean_otel_globals
) -> None:
    from opentelemetry import trace

    first = configure_telemetry(_settings(), set_global=True)
    first.shutdown()
    with pytest.warns(RuntimeWarning, match="tracer, meter, logger provider was already set"):
        second = configure_telemetry(_settings(), set_global=True)
    try:
        assert second is not first
        assert trace.get_tracer_provider() is first.tracer_provider
        # Logging does not depend on the set-once globals: the handler is bound to this call.
        assert _otel_handlers() == [second.logging_handler]
    finally:
        second.shutdown()
    assert _otel_handlers() == []


def test_set_global_reports_a_provider_another_component_already_installed(
    memory_otlp, clean_otel_globals
) -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    foreign = TracerProvider()
    trace.set_tracer_provider(foreign)
    with pytest.warns(RuntimeWarning, match="tracer provider was already set"):
        providers = configure_telemetry(_settings(), set_global=True)
    try:
        assert trace.get_tracer_provider() is foreign
        assert providers.tracer_provider is not foreign
    finally:
        providers.shutdown()


def test_capture_logs_false_still_correlates_but_attaches_no_handler(
    memory_otlp, clean_otel_globals
) -> None:
    providers = configure_telemetry(_settings(), set_global=True, capture_logs=False)
    try:
        assert providers.logging_handler is None
        assert _otel_handlers() == []
        assert providers.logging_instrumentor.is_instrumented_by_opentelemetry
    finally:
        providers.shutdown()


def test_without_set_global_nothing_global_or_on_the_root_logger_changes(
    memory_otlp, clean_otel_globals
) -> None:
    from opentelemetry import _logs, trace

    before = list(logging.getLogger().handlers)
    providers = configure_telemetry(_settings())
    try:
        assert providers.logging_handler is None and providers.logging_instrumentor is None
        assert logging.getLogger().handlers == before
        assert trace.get_tracer_provider() is not providers.tracer_provider
        assert _logs.get_logger_provider() is not providers.logger_provider
        assert otel_module._ACTIVE_GLOBAL is None
    finally:
        providers.shutdown()


def test_logging_handler_falls_back_to_the_sdk_handler_when_the_instrumentation_has_none(
    monkeypatch,
) -> None:
    from opentelemetry.sdk import _logs as sdk_logs

    real_require = otel_module.require

    def require(module: str, *, extra: str):
        if module == "opentelemetry.instrumentation.logging.handler":
            raise RuntimeError("older instrumentation without a handler module")
        return real_require(module, extra=extra)

    assert otel_module._logging_handler_class().__module__ != sdk_logs.LoggingHandler.__module__
    monkeypatch.setattr(otel_module, "require", require)
    assert otel_module._logging_handler_class() is sdk_logs.LoggingHandler
