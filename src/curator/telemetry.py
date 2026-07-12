"""Curator's optional OpenTelemetry (traces + metrics) and Elasticsearch structured-logging legs.

Both legs are independently no-op when their configuration is absent -- local dev and CI set neither
``AlloyEndpoint`` (see :class:`~curator.settings.Settings`) nor ``ElasticsearchNode`` /
``ElasticsearchUsername`` / ``ElasticsearchPassword`` -- and telemetry must never prevent the app from
starting: :func:`configure_telemetry` wraps each leg in its own broad ``except Exception`` so a bad
endpoint, an unreachable collector, or any other telemetry-only failure is logged to stderr and swallowed
rather than raised into ``create_app``. This mirrors the fleet convention (see the workspace root
``AGENTS.md``): OTLP gRPC to Grafana Alloy for traces and metrics with ``service.name`` = ``"curator"``,
``/health`` excluded from tracing, and Elasticsearch-shipped logs carrying ``service.name`` and a flat
``log.level`` field (mirroring what the Churches Node app ships).

:func:`configure_telemetry` is called once per app from :func:`curator.app.create_app`. Because each
gunicorn worker process calls the factory independently, per-worker OTel initialization comes for free --
nothing here spawns exporters or background threads at import time, only when the factory actually runs.
The *global* provider/instrumentation registration (:func:`opentelemetry.trace.set_tracer_provider`,
:func:`opentelemetry.metrics.set_meter_provider`, and the psycopg/requests library instrumentors, all of
which are process-wide state) is guarded by a module-level flag so calling ``create_app`` more than once in
the same process -- as the test suite does -- never stacks a second provider on top of the first. Per-app
instrumentation (``FastAPIInstrumentor.instrument_app``) is not process-wide state, so it runs for every
app instance.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import QueueHandler, QueueListener
from queue import SimpleQueue
from typing import Any

from elasticsearch import Elasticsearch
from fastapi import FastAPI
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from curator.settings import Settings

SERVICE_NAME_VALUE = "curator"
_HEALTH_EXCLUDED_URLS = "health"
_ES_INDEX_PREFIX = "curator-logs"

_otel_lock = threading.Lock()
_otel_configured = False

_es_logging_lock = threading.Lock()
_es_logging_configured = False


def configure_telemetry(app: FastAPI, settings: Settings) -> None:
    """Wire up Curator's telemetry legs, never allowing a telemetry failure to prevent app startup.

    :param app: The just-constructed FastAPI app to instrument.
    :param settings: The resolved :class:`~curator.settings.Settings`; each leg reads only its own
        fields and is skipped entirely when they are absent.
    """
    # Deliberately broad: any exception from either leg (a bad endpoint, an unreachable collector, a
    # misconfigured client, ...) must be logged and swallowed, never allowed to crash app startup.
    try:
        _configure_tracing_and_metrics(app, settings)
    except Exception as exc:
        print(f"curator.telemetry: OTLP telemetry setup failed, continuing without it: {exc}", file=sys.stderr)

    try:
        _configure_elasticsearch_logging(settings)
    except Exception as exc:
        print(f"curator.telemetry: Elasticsearch logging setup failed, continuing without it: {exc}", file=sys.stderr)


def _configure_tracing_and_metrics(app: FastAPI, settings: Settings) -> None:
    """Configure OTLP traces + metrics and instrument FastAPI/psycopg/requests, iff ``alloy_endpoint`` is set.

    :param app: The FastAPI app to instrument (per-instance; not process-wide state).
    :param settings: The resolved settings; only ``alloy_endpoint`` is consulted.
    """
    if not settings.alloy_endpoint:
        return

    _register_otlp_providers(settings.alloy_endpoint)
    _instrument_app(app)


def _register_otlp_providers(alloy_endpoint: str) -> None:
    """Register the process-wide TracerProvider/MeterProvider and library instrumentors, exactly once.

    Guarded by :data:`_otel_configured` so repeated calls in the same process (multiple ``create_app``
    calls, as in tests) never stack a second provider or double-instrument psycopg/requests.

    :param alloy_endpoint: The Grafana Alloy OTLP gRPC endpoint.
    """
    global _otel_configured
    with _otel_lock:
        if _otel_configured:
            return

        resource = Resource.create({SERVICE_NAME: SERVICE_NAME_VALUE})

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=alloy_endpoint)))
        trace.set_tracer_provider(tracer_provider)

        meter_provider = MeterProvider(
            resource=resource,
            metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=alloy_endpoint))],
        )
        metrics.set_meter_provider(meter_provider)

        psycopg_instrumentor = PsycopgInstrumentor()
        if not psycopg_instrumentor.is_instrumented_by_opentelemetry:
            psycopg_instrumentor.instrument()

        requests_instrumentor = RequestsInstrumentor()
        if not requests_instrumentor.is_instrumented_by_opentelemetry:
            requests_instrumentor.instrument()

        _otel_configured = True


def _instrument_app(app: FastAPI) -> None:
    """Instrument a single FastAPI app instance, excluding ``/health`` from tracing (fleet convention).

    :param app: The FastAPI app to instrument.
    """
    FastAPIInstrumentor.instrument_app(app, excluded_urls=_HEALTH_EXCLUDED_URLS)


def _configure_elasticsearch_logging(settings: Settings) -> None:
    """Attach a root-logger handler shipping ECS-ish JSON docs to Elasticsearch, iff fully configured.

    The node URL and both basic-auth credentials must all be present -- a partially configured leg is
    treated the same as an absent one (disabled), never a startup error. Guarded by
    :data:`_es_logging_configured` so repeated calls never attach a second handler.

    :param settings: The resolved settings; ``elasticsearch_node``, ``elasticsearch_username``, and
        ``elasticsearch_password`` are consulted.
    """
    if not (settings.elasticsearch_node and settings.elasticsearch_username and settings.elasticsearch_password):
        return

    global _es_logging_configured
    with _es_logging_lock:
        if _es_logging_configured:
            return

        client = Elasticsearch(
            settings.elasticsearch_node,
            basic_auth=(settings.elasticsearch_username, settings.elasticsearch_password),
        )
        handler = _ElasticsearchLogHandler(client)

        # A QueueHandler/QueueListener pair moves the actual ES `index` call onto a background thread, so a
        # slow or unreachable Elasticsearch node can never block the request thread that emitted the log.
        log_queue: SimpleQueue[logging.LogRecord] = SimpleQueue()
        queue_handler = QueueHandler(log_queue)
        listener = QueueListener(log_queue, handler, respect_handler_level=True)
        listener.start()

        root_logger = logging.getLogger()
        root_logger.addHandler(queue_handler)
        root_logger.setLevel(logging.INFO)

        _es_logging_configured = True


def format_log_record(record: logging.LogRecord) -> dict[str, Any]:
    """Format a stdlib :class:`~logging.LogRecord` into the ECS-ish JSON doc shipped to Elasticsearch.

    ``service.name`` and a *flat* ``log.level`` key (not a nested ``log: {level: ...}`` object) match what
    the Grafana Logs dashboard expects, mirroring the Churches Node app's Elasticsearch documents.

    :param record: The log record to format.
    :returns: A JSON-serializable document.
    """
    doc: dict[str, Any] = {
        "@timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
        "message": record.getMessage(),
        "log.level": record.levelname,
        "service.name": SERVICE_NAME_VALUE,
        "logger.name": record.name,
    }
    if record.exc_info:
        doc["error.stack_trace"] = logging.Formatter().formatException(record.exc_info)
    return doc


def _log_index_name(now: datetime | None = None) -> str:
    """Build the day-bucketed Elasticsearch index name a log document is written to.

    :param now: The timestamp to bucket by; defaults to the current UTC time.
    :returns: The index name, e.g. ``"curator-logs-2026.07.11"``.
    """
    moment = now or datetime.now(timezone.utc)
    return f"{_ES_INDEX_PREFIX}-{moment:%Y.%m.%d}"


class _ElasticsearchLogHandler(logging.Handler):
    """Ships formatted log records to Elasticsearch.

    Intended to run at the tail of a :class:`~logging.handlers.QueueListener` pipeline (see
    :func:`_configure_elasticsearch_logging`) so the ``emit`` call below -- which performs the actual
    network request -- never runs on a request-handling thread. ``emit`` additionally swallows every
    exception itself as a second line of defense: a down or slow Elasticsearch node must never raise into
    application code, and must never take down the background listener thread either.
    """

    def __init__(self, client: Elasticsearch) -> None:
        """Build the handler.

        :param client: The Elasticsearch client to index documents through.
        """
        super().__init__()
        self._client = client

    def emit(self, record: logging.LogRecord) -> None:
        """Index one formatted log document, swallowing any failure.

        :param record: The log record to ship.
        """
        with contextlib.suppress(Exception):
            self._client.index(index=_log_index_name(), document=format_log_record(record))
