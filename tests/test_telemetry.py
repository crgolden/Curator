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
from datetime import datetime, timezone
from typing import ClassVar

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

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

    def instrument(self):
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

    def index(self, *, index, document):
        pass


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
    assert doc["log.level"] == "WARNING"
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


def test_log_index_name_is_day_bucketed():
    moment = datetime(2026, 7, 11, 3, 30, tzinfo=timezone.utc)
    assert telemetry._log_index_name(moment) == "curator-logs-2026.07.11"


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
