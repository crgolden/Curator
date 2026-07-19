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
import sys
from datetime import datetime
from typing import Any, ClassVar

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from opentelemetry.instrumentation.httpx import RequestInfo
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import curator.telemetry as telemetry
from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from curator.settings import Settings
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator

_SETTINGS_NO_TELEMETRY = Settings(
    oidc_authority="https://identity.example.test",
    token_key="token-key",
    database_url="postgresql://unused",
)


class _FakeExporter:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


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
    def __init__(self, exporter):
        self.exporter = exporter


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


class _FakeMetricsNamespace:
    def __init__(self):
        self.meter_providers = []

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


# ---------------------------------------------------------------------------------------------------
# No-op when settings are absent.
# ---------------------------------------------------------------------------------------------------


def test_configure_telemetry_is_a_noop_when_settings_absent(monkeypatch):
    monkeypatch.setattr(telemetry, "_otel_configured", False)
    monkeypatch.setattr(telemetry, "_es_logging_configured", False)

    telemetry.configure_telemetry(app=object(), settings=_SETTINGS_NO_TELEMETRY)
    telemetry.configure_telemetry(app=object(), settings=_SETTINGS_NO_TELEMETRY)

    assert telemetry._otel_configured is False
    assert telemetry._es_logging_configured is False


def test_configure_telemetry_never_raises_even_if_a_leg_blows_up(monkeypatch):
    monkeypatch.setattr(telemetry, "_otel_configured", False)
    monkeypatch.setattr(telemetry, "_es_logging_configured", False)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated telemetry failure")

    monkeypatch.setattr(telemetry, "_configure_tracing_and_metrics", _boom)
    monkeypatch.setattr(telemetry, "_configure_elasticsearch_logging", _boom)

    # Must not raise.
    telemetry.configure_telemetry(app=object(), settings=_SETTINGS_NO_TELEMETRY)


# ---------------------------------------------------------------------------------------------------
# OTLP provider registration: idempotent, /health excluded from tracing.
# ---------------------------------------------------------------------------------------------------


def test_register_otlp_providers_registers_exactly_once_across_repeated_calls(monkeypatch):
    _patch_otlp_collaborators(monkeypatch)

    telemetry._register_otlp_providers("https://alloy.example.test:4317")
    telemetry._register_otlp_providers("https://alloy.example.test:4317")

    assert _FakeTracerProvider.instances == 1
    assert _FakeMeterProvider.instances == 1
    assert _FakeInstrumentor.instrument_calls == 2  # psycopg + requests, once each


def test_configure_tracing_and_metrics_noop_when_alloy_endpoint_absent(monkeypatch):
    _patch_otlp_collaborators(monkeypatch)

    telemetry._configure_tracing_and_metrics(app=object(), settings=_SETTINGS_NO_TELEMETRY)

    assert _FakeTracerProvider.instances == 0
    assert _FakeMeterProvider.instances == 0
    assert telemetry._otel_configured is False


def test_instrument_app_excludes_health_from_tracing(monkeypatch):
    monkeypatch.setattr(telemetry, "FastAPIInstrumentor", _FakeFastAPIInstrumentor)
    _FakeFastAPIInstrumentor.calls = []
    app = object()

    telemetry._instrument_app(app)

    assert len(_FakeFastAPIInstrumentor.calls) == 1
    called_app, kwargs = _FakeFastAPIInstrumentor.calls[0]
    assert called_app is app
    assert kwargs["excluded_urls"] == "health"


# ---------------------------------------------------------------------------------------------------
# Elasticsearch logging leg: no-op unless fully configured, idempotent, log-doc formatting.
# ---------------------------------------------------------------------------------------------------


class _FakeElasticsearchClient:
    instances = 0

    def __init__(self, *args, **kwargs):
        type(self).instances += 1
        self.args = args
        self.kwargs = kwargs
        self.index_calls: list[dict[str, Any]] = []

    def index(self, *, index, document, op_type=None, require_data_stream=None):
        self.index_calls.append(
            {"index": index, "document": document, "op_type": op_type, "require_data_stream": require_data_stream}
        )


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
        "elasticsearch_username": "curator",
        "elasticsearch_password": "secret",
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
    transport_logger = logging.getLogger("elastic_transport")
    es_logger = logging.getLogger("elasticsearch")
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


def test_configure_elasticsearch_logging_noop_when_only_username_set(monkeypatch):
    _patch_es_collaborators(monkeypatch)
    settings = _settings_with_es(elasticsearch_password=None)

    telemetry._configure_elasticsearch_logging(settings)

    assert _FakeElasticsearchClient.instances == 0
    assert telemetry._es_logging_configured is False


def test_format_log_record_produces_flat_log_level_and_service_name():
    record = logging.LogRecord(
        name="curator.psn_routes",
        level=logging.WARNING,
        pathname=__file__,
        lineno=1,
        msg="something happened: %s",
        args=("detail",),
        exc_info=None,
    )

    doc = telemetry.format_log_record(record)

    assert doc["message"] == "something happened: detail"
    assert doc["log.level"] == "Warning"  # fleet's Serilog/ECS spelling, not Python's own "WARNING"
    assert doc["service.name"] == "curator"
    assert doc["logger.name"] == "curator.psn_routes"
    assert "log" not in doc  # flat key, never a nested `log: {level: ...}` object
    assert "error.stack_trace" not in doc
    datetime.fromisoformat(doc["@timestamp"])  # parses without raising


def test_format_log_record_includes_stack_trace_on_exception():
    try:
        raise ValueError("boom")
    except ValueError:
        exc_info = sys.exc_info()
        record = logging.LogRecord(
            name="curator.app",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=exc_info,
        )

    doc = telemetry.format_log_record(record)

    assert "ValueError: boom" in doc["error.stack_trace"]


def test_format_log_record_maps_every_python_level_to_the_fleet_vocabulary():
    for level, expected in (
        (logging.DEBUG, "Debug"),
        (logging.INFO, "Information"),
        (logging.WARNING, "Warning"),
        (logging.ERROR, "Error"),
        (logging.CRITICAL, "Fatal"),
    ):
        record = logging.LogRecord(
            name="curator.app", level=level, pathname=__file__, lineno=1, msg="x", args=(), exc_info=None
        )
        assert telemetry.format_log_record(record)["log.level"] == expected


def test_elasticsearch_log_handler_emits_a_create_write_to_the_data_stream():
    """The target must be `logs-app-curator` (matching the Grafana `logs-app-*` pattern and
    Elasticsearch's built-in `logs` index template) written with `op_type="create"` and
    `require_data_stream=True` -- data streams are append-only and reject the default "index" op type,
    and `require_data_stream` fails loudly instead of silently falling back to a bare index.
    """
    client = _FakeElasticsearchClient()
    handler = telemetry._ElasticsearchLogHandler(client)
    record = logging.LogRecord(
        name="curator.app",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hello",
        args=(),
        exc_info=None,
    )

    handler.emit(record)

    assert len(client.index_calls) == 1
    call = client.index_calls[0]
    assert call["index"] == "logs-app-curator"
    assert call["op_type"] == "create"
    assert call["require_data_stream"] is True
    assert call["document"]["message"] == "hello"


def test_elasticsearch_log_handler_swallows_index_failures():
    class _FailingClient(_FakeElasticsearchClient):
        def index(self, **kwargs):
            raise RuntimeError("elasticsearch unreachable")

    handler = telemetry._ElasticsearchLogHandler(_FailingClient())
    record = logging.LogRecord(
        name="curator.app", level=logging.INFO, pathname=__file__, lineno=1, msg="x", args=(), exc_info=None
    )

    handler.emit(record)  # must not raise


# ---------------------------------------------------------------------------------------------------
# End-to-end through create_app: telemetry never breaks app construction or /health.
# ---------------------------------------------------------------------------------------------------


def test_create_app_health_check_unaffected_by_telemetry_wiring(monkeypatch):
    monkeypatch.setattr(telemetry, "_otel_configured", False)
    monkeypatch.setattr(telemetry, "_es_logging_configured", False)

    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    app = create_app(
        _SETTINGS_NO_TELEMETRY,
        repository=repository,
        token_crypto=crypto,
        agent_factory=FakeAgentFactory(repository, crypto),
        token_validator=FakeTokenValidator(),
    )
    client = TestClient(app)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.text == "Healthy"


# ---------------------------------------------------------------------------------------------------
# _redact_rawg_key_from_span: RAWG's API key must never land in a Tempo span attribute.
# ---------------------------------------------------------------------------------------------------


def _traced_span(url_attribute_keys=("http.url", "url.full")):
    """Start and immediately end a real recording span with URL attributes pre-set, mimicking what
    HTTPXClientInstrumentor does before invoking the async_request_hook -- returns (span, exporter)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    attributes = {key: "https://api.rawg.io/api/games?key=super-secret&search=Foo" for key in url_attribute_keys}
    span = tracer.start_span("test-span", attributes=attributes)
    return span, exporter


async def test_redact_rawg_key_from_span_strips_key_param_for_rawg_host():
    span, exporter = _traced_span()
    request_info = RequestInfo(
        method=b"GET",
        url=httpx.URL("https://api.rawg.io/api/games?key=super-secret&search=Foo"),
        headers=None,
        stream=None,
        extensions=None,
    )

    await telemetry._redact_rawg_key_from_span(span, request_info)
    span.end()

    (recorded,) = exporter.get_finished_spans()
    for attribute_key in ("http.url", "url.full"):
        assert "super-secret" not in recorded.attributes[attribute_key]
        assert "search=Foo" in recorded.attributes[attribute_key]


async def test_redact_rawg_key_from_span_leaves_non_rawg_hosts_untouched():
    span, exporter = _traced_span()
    request_info = RequestInfo(
        method=b"GET",
        url=httpx.URL("https://opencritic-api.p.rapidapi.com/game?platforms=ps5"),
        headers=None,
        stream=None,
        extensions=None,
    )

    await telemetry._redact_rawg_key_from_span(span, request_info)
    span.end()

    (recorded,) = exporter.get_finished_spans()
    assert recorded.attributes["http.url"] == "https://api.rawg.io/api/games?key=super-secret&search=Foo"


async def test_redact_rawg_key_from_span_noop_when_span_not_recording():
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer(__name__)
    span = tracer.start_span("test-span", attributes={"http.url": "https://api.rawg.io/api/games?key=secret"})
    span.end()  # already ended -- is_recording() is now False

    request_info = RequestInfo(
        method=b"GET",
        url=httpx.URL("https://api.rawg.io/api/games?key=secret"),
        headers=None,
        stream=None,
        extensions=None,
    )
    await telemetry._redact_rawg_key_from_span(span, request_info)  # must not raise
