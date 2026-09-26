"""Tests for curator.telemetry: no-op-when-unset behavior, idempotency, and the Elasticsearch log-doc
formatter. Every OTel/Elasticsearch collaborator is a hand-written fake -- no ``unittest.mock``, no live
OTLP collector, no live Elasticsearch node, matching the rest of this suite's style.

``curator.telemetry`` keeps a few module-level flags (``_otel_configured``, ``_es_logging_configured``) so
repeated ``create_app`` calls never stack a second provider or handler -- exactly what makes ``create_app``
safe to call more than once in the same process, as this whole test suite does. Tests that flip those
flags reset them via ``monkeypatch.setattr`` rather than direct assignment, so the change never leaks into
another test module.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from datetime import datetime
from typing import Any, ClassVar, NamedTuple
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.httpx import RequestInfo
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import curator.telemetry as telemetry
from curator.app import HEALTH_PATH, HEALTHY_BODY, create_app
from curator.persistence.crypto import TokenCrypto
from curator.settings import Settings
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator
from test_values import lowercase_token, new_opaque_token

_SETTINGS_NO_TELEMETRY = Settings(
    oidc_authority="https://identity.example.test",
    token_key="token-key",
    database_url="postgresql://unused",
)

_SETTINGS_WITH_TELEMETRY = Settings(
    oidc_authority="https://identity.example.test",
    token_key="token-key",
    database_url="postgresql://unused",
    alloy_endpoint="https://alloy.example.test:4317",
)


class _FakeExporter:
    all_kwargs: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).all_kwargs.append(kwargs)


class _FakeSpanProcessor:
    def __init__(self, exporter):
        self.exporter = exporter


class _FakeTracerProvider:
    instances = 0

    def __init__(self, **kwargs):
        type(self).instances += 1
        self.processors = []

    def add_span_processor(self, processor):
        self.processors.append(processor)


class _FakeMetricReader:
    last_kwargs: ClassVar[dict[str, Any]] = {}

    def __init__(self, exporter, **kwargs):
        self.exporter = exporter
        self.kwargs = kwargs
        type(self).last_kwargs = kwargs


class _FakeMeterProvider:
    instances = 0

    def __init__(self, **kwargs):
        type(self).instances += 1


class _FakeInstrumentor:
    instrument_calls = 0

    def __init__(self):
        self.is_instrumented_by_opentelemetry = False

    def instrument(self, **kwargs):
        type(self).instrument_calls += 1
        self.is_instrumented_by_opentelemetry = True


class _FakeTraceNamespace:
    def __init__(self):
        self.tracer_providers = []

    def set_tracer_provider(self, provider):
        self.tracer_providers.append(provider)

    def get_tracer_provider(self):
        return self.tracer_providers[-1]


class _FakeMetricsNamespace:
    def __init__(self):
        self.meter_providers = []

    def get_meter_provider(self):
        return self.meter_providers[-1]

    def set_meter_provider(self, provider):
        self.meter_providers.append(provider)


class _FakeFastAPIInstrumentor:
    calls: ClassVar[list] = []

    @staticmethod
    def instrument_app(app, **kwargs):
        _FakeFastAPIInstrumentor.calls.append((app, kwargs))


def _patch_otlp_collaborators(monkeypatch):
    """Replace every OTel collaborator ``_register_otlp_providers`` touches with an in-memory fake."""
    monkeypatch.setattr(telemetry, "TracerProvider", _FakeTracerProvider)
    monkeypatch.setattr(telemetry, "MeterProvider", _FakeMeterProvider)
    monkeypatch.setattr(telemetry, "OTLPSpanExporter", _FakeExporter)
    monkeypatch.setattr(telemetry, "OTLPMetricExporter", _FakeExporter)
    monkeypatch.setattr(telemetry, "BatchSpanProcessor", _FakeSpanProcessor)
    monkeypatch.setattr(telemetry, "PeriodicExportingMetricReader", _FakeMetricReader)
    monkeypatch.setattr(telemetry, "PsycopgInstrumentor", _FakeInstrumentor)
    monkeypatch.setattr(telemetry, "HTTPXClientInstrumentor", _FakeInstrumentor)
    monkeypatch.setattr(telemetry, "trace", _FakeTraceNamespace())
    monkeypatch.setattr(telemetry, "metrics", _FakeMetricsNamespace())
    monkeypatch.setattr(telemetry, "_otel_configured", False)
    _FakeTracerProvider.instances = 0
    _FakeMeterProvider.instances = 0
    _FakeInstrumentor.instrument_calls = 0
    _FakeMetricReader.last_kwargs = {}
    _FakeExporter.all_kwargs = []


def test_configure_telemetry_is_a_noop_when_settings_absent(monkeypatch):
    monkeypatch.setattr(telemetry, "_otel_configured", False)
    monkeypatch.setattr(telemetry, "_es_logging_configured", False)

    telemetry.configure_telemetry(app=object(), settings=_SETTINGS_NO_TELEMETRY)
    telemetry.configure_telemetry(app=object(), settings=_SETTINGS_NO_TELEMETRY)

    assert telemetry._otel_configured is False
    assert telemetry._es_logging_configured is False


def test_configure_telemetry_lets_a_failing_otlp_leg_stop_startup(monkeypatch):
    monkeypatch.setattr(telemetry, "_otel_configured", False)
    monkeypatch.setattr(telemetry, "_es_logging_configured", False)
    failure = RuntimeError(str(uuid4()))

    def _boom(*args, **kwargs):
        raise failure

    monkeypatch.setattr(telemetry, "_configure_tracing_and_metrics", _boom)

    with pytest.raises(RuntimeError) as raised:
        telemetry.configure_telemetry(app=object(), settings=_SETTINGS_NO_TELEMETRY)

    assert raised.value is failure


def test_configure_telemetry_lets_a_failing_elasticsearch_leg_stop_startup(monkeypatch):
    monkeypatch.setattr(telemetry, "_otel_configured", False)
    monkeypatch.setattr(telemetry, "_es_logging_configured", False)
    failure = RuntimeError(str(uuid4()))

    def _boom(*args, **kwargs):
        raise failure

    monkeypatch.setattr(telemetry, "_configure_elasticsearch_logging", _boom)

    with pytest.raises(RuntimeError) as raised:
        telemetry.configure_telemetry(app=object(), settings=_SETTINGS_NO_TELEMETRY)

    assert raised.value is failure


def test_register_otlp_providers_registers_exactly_once_across_repeated_calls(monkeypatch):
    _patch_otlp_collaborators(monkeypatch)

    telemetry._register_otlp_providers("https://alloy.example.test:4317")
    telemetry._register_otlp_providers("https://alloy.example.test:4317")

    assert _FakeTracerProvider.instances == 1
    assert _FakeMeterProvider.instances == 1
    assert _FakeInstrumentor.instrument_calls == 2


def test_register_otlp_providers_shortens_the_metric_export_interval(monkeypatch):
    """A gunicorn worker recycled between the SDK-default 60s ticks would report traces (flushed
    continuously via BatchSpanProcessor's own thread) but drop every metric -- shortening the interval
    narrows that window. See the `_METRIC_EXPORT_INTERVAL_MILLIS` comment for the full explanation.
    """
    _patch_otlp_collaborators(monkeypatch)

    telemetry._register_otlp_providers("https://alloy.example.test:4317")

    assert _FakeMetricReader.last_kwargs == {"export_interval_millis": telemetry._METRIC_EXPORT_INTERVAL_MILLIS}
    assert telemetry._METRIC_EXPORT_INTERVAL_MILLIS < 60_000


def test_register_otlp_providers_gives_both_exporters_a_timeout_longer_than_the_grpc_default(monkeypatch):
    _patch_otlp_collaborators(monkeypatch)

    telemetry._register_otlp_providers("https://alloy.example.test:4317")

    assert len(_FakeExporter.all_kwargs) == 2, "one span exporter and one metric exporter"
    for kwargs in _FakeExporter.all_kwargs:
        assert kwargs["timeout"] == telemetry._EXPORT_TIMEOUT_SECONDS
    assert telemetry._EXPORT_TIMEOUT_SECONDS > 10


def test_configure_elasticsearch_logging_keeps_otlp_exporter_failures_out_of_elasticsearch(monkeypatch):
    _patch_es_collaborators(monkeypatch)
    settings = _settings_with_es()
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)

    try:
        telemetry._configure_elasticsearch_logging(settings)
        queue_handler = root_logger.handlers[-1]

        exporter_record = logging.LogRecord(
            telemetry._OTLP_EXPORTER_LOGGER, logging.ERROR, __file__, 1, "Failed to export metrics", None, None
        )
        application_record = logging.LogRecord(
            "curator.jobs.queue_publisher", logging.ERROR, __file__, 1, "a real failure", None, None
        )

        assert not queue_handler.filter(exporter_record)
        assert queue_handler.filter(application_record)
    finally:
        root_logger.handlers = original_handlers


def test_shutdown_telemetry_is_a_noop_when_never_configured(monkeypatch):
    monkeypatch.setattr(telemetry, "_otel_configured", False)
    tracer_provider_before = telemetry.trace.get_tracer_provider()
    meter_provider_before = telemetry.metrics.get_meter_provider()

    telemetry.shutdown_telemetry()

    assert telemetry.trace.get_tracer_provider() is tracer_provider_before
    assert telemetry.metrics.get_meter_provider() is meter_provider_before


def test_shutdown_telemetry_shuts_down_both_providers_when_configured(monkeypatch):
    _patch_otlp_collaborators(monkeypatch)
    telemetry._register_otlp_providers("https://alloy.example.test:4317")

    shutdown_calls: list[str] = []

    class _ShutdownTracerProvider:
        def shutdown(self):
            shutdown_calls.append("tracer")

    class _ShutdownMeterProvider:
        def shutdown(self):
            shutdown_calls.append("meter")

    telemetry.trace.set_tracer_provider(_ShutdownTracerProvider())
    telemetry.metrics.set_meter_provider(_ShutdownMeterProvider())

    telemetry.shutdown_telemetry()

    assert shutdown_calls == ["tracer", "meter"]


def test_shutdown_telemetry_swallows_a_failing_provider_shutdown(monkeypatch):
    _patch_otlp_collaborators(monkeypatch)
    telemetry._register_otlp_providers("https://alloy.example.test:4317")

    class _BoomProvider:
        def shutdown(self):
            raise RuntimeError("collector unreachable")

    telemetry.trace.set_tracer_provider(_BoomProvider())
    telemetry.metrics.set_meter_provider(_BoomProvider())

    telemetry.shutdown_telemetry()


def test_configure_tracing_and_metrics_noop_when_alloy_endpoint_absent(monkeypatch):
    _patch_otlp_collaborators(monkeypatch)

    telemetry._configure_tracing_and_metrics(app=object(), settings=_SETTINGS_NO_TELEMETRY)

    assert _FakeTracerProvider.instances == 0
    assert _FakeMeterProvider.instances == 0
    assert telemetry._otel_configured is False


def test_configure_tracing_and_metrics_selects_the_stable_http_and_database_semconv(monkeypatch):
    monkeypatch.delenv(telemetry._SEMCONV_STABILITY_OPT_IN_ENV, raising=False)
    monkeypatch.setattr(telemetry, "_register_otlp_providers", lambda endpoint: None)
    monkeypatch.setattr(telemetry, "_instrument_app", lambda app: None)

    telemetry._configure_tracing_and_metrics(app=object(), settings=_SETTINGS_WITH_TELEMETRY)

    assert os.environ[telemetry._SEMCONV_STABILITY_OPT_IN_ENV] == telemetry._SEMCONV_STABILITY_OPT_IN


def test_the_semconv_opt_in_is_the_stable_http_and_database_conventions():
    assert telemetry._SEMCONV_STABILITY_OPT_IN == "http,database"


def test_configure_tracing_and_metrics_selects_the_semconv_before_anything_is_instrumented(monkeypatch):
    monkeypatch.delenv(telemetry._SEMCONV_STABILITY_OPT_IN_ENV, raising=False)
    seen: list[str | None] = []

    def record(_):
        seen.append(os.environ.get(telemetry._SEMCONV_STABILITY_OPT_IN_ENV))

    monkeypatch.setattr(telemetry, "_register_otlp_providers", record)
    monkeypatch.setattr(telemetry, "_instrument_app", record)

    telemetry._configure_tracing_and_metrics(app=object(), settings=_SETTINGS_WITH_TELEMETRY)

    assert seen == [telemetry._SEMCONV_STABILITY_OPT_IN, telemetry._SEMCONV_STABILITY_OPT_IN]


def test_configure_tracing_and_metrics_leaves_an_explicit_semconv_value_alone(monkeypatch):
    monkeypatch.setenv(telemetry._SEMCONV_STABILITY_OPT_IN_ENV, "http/dup")
    monkeypatch.setattr(telemetry, "_register_otlp_providers", lambda endpoint: None)
    monkeypatch.setattr(telemetry, "_instrument_app", lambda app: None)

    telemetry._configure_tracing_and_metrics(app=object(), settings=_SETTINGS_WITH_TELEMETRY)

    assert os.environ[telemetry._SEMCONV_STABILITY_OPT_IN_ENV] == "http/dup"


def test_configure_tracing_and_metrics_does_not_select_a_semconv_when_telemetry_is_off(monkeypatch):
    monkeypatch.delenv(telemetry._SEMCONV_STABILITY_OPT_IN_ENV, raising=False)

    telemetry._configure_tracing_and_metrics(app=object(), settings=_SETTINGS_NO_TELEMETRY)

    assert telemetry._SEMCONV_STABILITY_OPT_IN_ENV not in os.environ


def test_instrument_app_excludes_health_from_tracing(monkeypatch):
    monkeypatch.setattr(telemetry, "FastAPIInstrumentor", _FakeFastAPIInstrumentor)
    _FakeFastAPIInstrumentor.calls = []
    app = object()

    telemetry._instrument_app(app)

    assert len(_FakeFastAPIInstrumentor.calls) == 1
    called_app, kwargs = _FakeFastAPIInstrumentor.calls[0]
    assert called_app is app
    assert kwargs["excluded_urls"] == telemetry._HEALTH_EXCLUDED_URLS


class _IndexCall(NamedTuple):
    data_stream: str
    document: dict[str, Any]
    op_type: str | None
    require_data_stream: bool | None


class _FakeElasticsearchClient:
    instances = 0

    def __init__(self, *args, **kwargs):
        type(self).instances += 1
        self.args = args
        self.kwargs = kwargs
        self.index_calls: list[_IndexCall] = []

    def index(self, *, index, document, op_type=None, require_data_stream=None):
        self.index_calls.append(_IndexCall(index, document, op_type, require_data_stream))


class _FakeQueueListener:
    starts = 0

    def __init__(self, queue, *handlers, **kwargs):
        self.queue = queue
        self.handlers = handlers

    def start(self):
        type(self).starts += 1


def _patch_es_collaborators(monkeypatch):
    """Fake the network-touching/thread-spawning collaborators, but leave the real ``logging`` module
    alone -- ``root_logger.addHandler``/``setLevel`` are cheap, well-understood stdlib calls, and any
    handler this attaches is a ``QueueHandler`` feeding the faked (never-started-for-real) listener above,
    so it never actually ships anything. Tests that exercise this restore the root logger's handler list
    themselves.
    """
    monkeypatch.setattr(telemetry, "Elasticsearch", _FakeElasticsearchClient)
    monkeypatch.setattr(telemetry, "QueueListener", _FakeQueueListener)
    monkeypatch.setattr(telemetry, "_es_logging_configured", False)
    _FakeElasticsearchClient.instances = 0
    _FakeQueueListener.starts = 0


def _settings_with_es(**overrides):
    values = {
        "oidc_authority": "https://identity.example.test",
        "token_key": "token-key",
        "database_url": "postgresql://unused",
        "elasticsearch_node": "https://es.example.test:9200",
        "elasticsearch_username": lowercase_token(),
        "elasticsearch_password": new_opaque_token(),
    }
    values.update(overrides)
    return Settings(**values)


def test_configure_elasticsearch_logging_registers_exactly_once_across_repeated_calls(monkeypatch):
    _patch_es_collaborators(monkeypatch)
    settings = _settings_with_es()
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)

    try:
        telemetry._configure_elasticsearch_logging(settings)
        telemetry._configure_elasticsearch_logging(settings)

        assert _FakeElasticsearchClient.instances == 1
        assert _FakeQueueListener.starts == 1
        assert len(root_logger.handlers) == len(original_handlers) + 1
    finally:
        root_logger.handlers = original_handlers


def test_configure_elasticsearch_logging_disables_propagation_on_the_es_client_loggers(monkeypatch):
    """`elastic_transport`/`elasticsearch` log every HTTP call the ES client makes, including the ones
    this handler issues to ship a log record -- left propagating to root, each shipped record would
    produce a new log from these loggers, which would then also get shipped, forever. This must never
    reach root (found live in production: 1.6M+ self-referential docs before the pipeline died).
    """
    _patch_es_collaborators(monkeypatch)
    settings = _settings_with_es()
    root_logger = logging.getLogger()
    original_handlers = list(root_logger.handlers)
    transport_logger = logging.getLogger(telemetry.ELASTIC_TRANSPORT_LOGGER)
    es_logger = logging.getLogger(telemetry.ELASTICSEARCH_LOGGER)
    original_transport_propagate = transport_logger.propagate
    original_es_propagate = es_logger.propagate

    try:
        telemetry._configure_elasticsearch_logging(settings)

        assert transport_logger.propagate is False
        assert es_logger.propagate is False
    finally:
        root_logger.handlers = original_handlers
        transport_logger.propagate = original_transport_propagate
        es_logger.propagate = original_es_propagate


def test_configure_elasticsearch_logging_noop_when_node_absent(monkeypatch):
    _patch_es_collaborators(monkeypatch)
    settings = _settings_with_es(elasticsearch_node=None)

    telemetry._configure_elasticsearch_logging(settings)

    assert _FakeElasticsearchClient.instances == 0
    assert telemetry._es_logging_configured is False


def test_configure_elasticsearch_logging_refuses_a_node_without_a_password(monkeypatch):
    _patch_es_collaborators(monkeypatch)
    settings = _settings_with_es(elasticsearch_password=None)

    with pytest.raises(ValueError, match=re.escape(telemetry.ELASTICSEARCH_CREDENTIALS_MISSING)):
        telemetry._configure_elasticsearch_logging(settings)

    assert _FakeElasticsearchClient.instances == 0
    assert telemetry._es_logging_configured is False


def test_the_document_fields_are_the_flat_names_the_grafana_logs_dashboard_reads():
    assert (
        telemetry.TIMESTAMP_FIELD,
        telemetry.MESSAGE_FIELD,
        telemetry.LOG_LEVEL_FIELD,
        telemetry.SERVICE_NAME_FIELD,
        telemetry.LOGGER_NAME_FIELD,
        telemetry.STACK_TRACE_FIELD,
    ) == ("@timestamp", "message", "log.level", "service.name", "logger.name", "error.stack_trace")
    assert telemetry.SERVICE_NAME_VALUE == "crgolden-curator"


def test_format_log_record_produces_flat_log_level_and_service_name():
    logger_name = f"{lowercase_token()}.{lowercase_token()}"
    detail = lowercase_token()
    record = logging.LogRecord(
        name=logger_name,
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="%s",
        args=(detail,),
        exc_info=None,
    )

    doc = telemetry.format_log_record(record)

    assert doc[telemetry.MESSAGE_FIELD] == detail
    assert doc[telemetry.LOG_LEVEL_FIELD] == telemetry._LEVEL_NAMES[logging.getLevelName(logging.WARNING)]
    assert doc[telemetry.SERVICE_NAME_FIELD] == telemetry.SERVICE_NAME_VALUE
    assert doc[telemetry.LOGGER_NAME_FIELD] == logger_name
    assert telemetry.STACK_TRACE_FIELD not in doc
    assert set(doc) == {
        telemetry.TIMESTAMP_FIELD,
        telemetry.MESSAGE_FIELD,
        telemetry.LOG_LEVEL_FIELD,
        telemetry.SERVICE_NAME_FIELD,
        telemetry.LOGGER_NAME_FIELD,
    }


def test_format_log_record_timestamp_is_parseable_iso_8601():
    record = logging.LogRecord(
        name="curator.psn_routes",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="something happened",
        args=(),
        exc_info=None,
    )

    doc = telemetry.format_log_record(record)

    assert datetime.fromisoformat(doc[telemetry.TIMESTAMP_FIELD]).tzinfo is not None


def test_format_log_record_includes_stack_trace_on_exception():
    failure = new_opaque_token()
    try:
        raise ValueError(failure)
    except ValueError:
        exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="curator.app",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg=lowercase_token(),
            args=(),
            exc_info=exc_info,
        )

    doc = telemetry.format_log_record(record)

    assert f"{ValueError.__name__}: {failure}" in doc[telemetry.STACK_TRACE_FIELD]


def test_the_level_names_are_the_fleet_serilog_vocabulary():
    assert telemetry._LEVEL_NAMES == {
        "DEBUG": "Debug",
        "INFO": "Information",
        "WARNING": "Warning",
        "ERROR": "Error",
        "CRITICAL": "Fatal",
    }


@pytest.mark.parametrize("level", [logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR, logging.CRITICAL])
def test_format_log_record_maps_every_python_level_to_the_fleet_vocabulary(level):
    record = logging.LogRecord(
        name=lowercase_token(), level=level, pathname=__file__, lineno=1, msg=lowercase_token(), args=(), exc_info=None
    )

    assert (
        telemetry.format_log_record(record)[telemetry.LOG_LEVEL_FIELD]
        == telemetry._LEVEL_NAMES[logging.getLevelName(level)]
    )


def test_elasticsearch_log_handler_emits_a_create_write_to_the_data_stream():
    """The target must be `logs-app-curator` (matching the Grafana `logs-app-*` pattern and
    Elasticsearch's built-in `logs` index template) written with `op_type="create"` and
    `require_data_stream=True` -- data streams are append-only and reject the default "index" op type,
    and `require_data_stream` fails loudly instead of silently falling back to a bare index.
    """
    client = _FakeElasticsearchClient()
    handler = telemetry._ElasticsearchLogHandler(client)
    message = lowercase_token()
    record = logging.LogRecord(
        name=lowercase_token(),
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )

    handler.emit(record)

    assert telemetry._ES_DATA_STREAM == "logs-app-curator"
    assert telemetry._ES_CREATE_OP_TYPE == "create"
    assert len(client.index_calls) == 1
    call = client.index_calls[0]
    assert call.data_stream == telemetry._ES_DATA_STREAM
    assert call.op_type == telemetry._ES_CREATE_OP_TYPE
    assert call.require_data_stream is True
    assert call.document[telemetry.MESSAGE_FIELD] == message


def test_elasticsearch_log_handler_swallows_index_failures():
    class _FailingClient(_FakeElasticsearchClient):
        def index(self, **kwargs):
            raise RuntimeError("elasticsearch unreachable")

    handler = telemetry._ElasticsearchLogHandler(_FailingClient())
    record = logging.LogRecord(
        name="curator.app", level=logging.INFO, pathname=__file__, lineno=1, msg="x", args=(), exc_info=None
    )

    handler.emit(record)


def test_elasticsearch_log_handler_counts_and_reports_a_failure_rather_than_dropping_it_silently(capsys):
    class _FailingClient(_FakeElasticsearchClient):
        def index(self, **kwargs):
            raise RuntimeError("elasticsearch unreachable")

    handler = telemetry._ElasticsearchLogHandler(_FailingClient())
    record = logging.LogRecord(
        name="curator.app", level=logging.INFO, pathname=__file__, lineno=1, msg="x", args=(), exc_info=None
    )

    handler.emit(record)

    assert handler.failure_count == 1
    assert "elasticsearch unreachable" in capsys.readouterr().err, (
        "a silently suppressed failure makes 'Elasticsearch is refusing our writes' and 'there was "
        "nothing to log' the same observation"
    )


def test_elasticsearch_log_handler_reports_the_first_failure_only_until_the_interval_is_reached(capsys):
    class _FailingClient(_FakeElasticsearchClient):
        def index(self, **kwargs):
            raise RuntimeError("elasticsearch unreachable")

    handler = telemetry._ElasticsearchLogHandler(_FailingClient())
    record = logging.LogRecord(
        name="curator.app", level=logging.INFO, pathname=__file__, lineno=1, msg="x", args=(), exc_info=None
    )
    emits = telemetry._ES_FAILURE_REPORT_EVERY

    for _ in range(emits):
        handler.emit(record)

    assert handler.failure_count == emits
    assert capsys.readouterr().err.count("Elasticsearch log shipping has failed") == 2, (
        "reporting every failure would turn an outage into a stderr flood"
    )


def test_create_app_health_check_unaffected_by_telemetry_wiring(monkeypatch):
    monkeypatch.setattr(telemetry, "_otel_configured", False)
    monkeypatch.setattr(telemetry, "_es_logging_configured", False)

    repository = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    app = create_app(
        _SETTINGS_NO_TELEMETRY,
        repository=repository,
        token_crypto=crypto,
        agent_factory=FakeAgentFactory(repository, crypto),
        token_validator=FakeTokenValidator(),
    )
    client = TestClient(app)

    response = client.get(HEALTH_PATH)

    assert response.status_code == 200
    assert response.text == HEALTHY_BODY


def _rawg_base() -> str:
    return f"https://{telemetry._REDACT_QUERY_PARAM_HOSTS[0]}/{lowercase_token()}"


def _traced_span(url, url_attribute_keys=telemetry._URL_SPAN_ATTRIBUTE_KEYS):
    """Start and immediately end a real recording span with URL attributes pre-set, mimicking what
    HTTPXClientInstrumentor does before invoking the async_request_hook -- returns (span, exporter)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    attributes = {key: url for key in url_attribute_keys}
    span = tracer.start_span("test-span", attributes=attributes)
    return span, exporter


def _ended_span(attributes):
    """A span that has already ended, so ``is_recording()`` is False -- returns (span, exporter)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    span = provider.get_tracer(__name__).start_span("test-span", attributes=attributes)
    span.end()
    return span, exporter


def _request_info(url):
    return RequestInfo(method=b"GET", url=httpx.URL(url), headers=None, stream=None, extensions=None)


async def test_redact_rawg_key_from_span_strips_key_param_for_rawg_host():
    base = _rawg_base()
    kept_param, kept_value = lowercase_token(), lowercase_token()
    url = str(httpx.URL(base, params={telemetry._REDACT_QUERY_PARAM_NAME: new_opaque_token(), kept_param: kept_value}))
    span, exporter = _traced_span(url)

    await telemetry._redact_rawg_key_from_span(span, _request_info(url))
    span.end()

    (recorded,) = exporter.get_finished_spans()
    sanitized = str(httpx.URL(base, params={kept_param: kept_value}))
    assert [recorded.attributes[key] for key in telemetry._URL_SPAN_ATTRIBUTE_KEYS] == [
        sanitized for _ in telemetry._URL_SPAN_ATTRIBUTE_KEYS
    ]


async def test_redact_rawg_key_from_span_leaves_non_rawg_hosts_untouched():
    rawg_url = str(httpx.URL(_rawg_base(), params={telemetry._REDACT_QUERY_PARAM_NAME: new_opaque_token()}))
    span, exporter = _traced_span(rawg_url)
    other_host_url = str(
        httpx.URL(
            f"https://{lowercase_token()}.example.test/{lowercase_token()}",
            params={telemetry._REDACT_QUERY_PARAM_NAME: new_opaque_token()},
        )
    )

    await telemetry._redact_rawg_key_from_span(span, _request_info(other_host_url))
    span.end()

    (recorded,) = exporter.get_finished_spans()
    assert recorded.attributes[telemetry._URL_SPAN_ATTRIBUTE_KEYS[0]] == rawg_url


async def test_redact_rawg_key_from_span_noop_when_span_not_recording():
    url = str(httpx.URL(_rawg_base(), params={telemetry._REDACT_QUERY_PARAM_NAME: new_opaque_token()}))
    span, exporter = _ended_span(attributes={telemetry._URL_SPAN_ATTRIBUTE_KEYS[0]: url})
    assert not span.is_recording()

    await telemetry._redact_rawg_key_from_span(span, _request_info(url))

    (recorded,) = exporter.get_finished_spans()
    assert recorded.attributes[telemetry._URL_SPAN_ATTRIBUTE_KEYS[0]] == url
