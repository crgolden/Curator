"""Curator's optional OpenTelemetry (traces + metrics) and Elasticsearch structured-logging legs.

Both legs are independently no-op when their configuration is absent -- local dev and CI set neither
``AlloyEndpoint`` (see :class:`~curator.settings.Settings`) nor ``ElasticsearchNode`` /
``ElasticsearchUsername`` / ``ElasticsearchPassword`` -- and telemetry must never prevent the app from
starting: :func:`configure_telemetry` wraps each leg in its own broad ``except Exception`` so a bad
endpoint, an unreachable collector, or any other telemetry-only failure is logged to stderr and swallowed
rather than raised into ``create_app``. This mirrors the fleet convention (see the workspace root
``AGENTS.md``): OTLP gRPC to Grafana Alloy for traces and metrics with ``service.name`` =
``"crgolden-curator"``, ``/health`` excluded from tracing, and Elasticsearch-shipped logs carrying
``service.name`` and a flat ``log.level`` field (mirroring what the Churches Node app ships).

:func:`configure_telemetry` is called once per app from :func:`curator.app.create_app`. Because each
gunicorn worker process calls the factory independently, per-worker OTel initialization comes for free --
nothing here spawns exporters or background threads at import time, only when the factory actually runs.
The *global* provider/instrumentation registration (:func:`opentelemetry.trace.set_tracer_provider`,
:func:`opentelemetry.metrics.set_meter_provider`, and the psycopg/httpx library instrumentors, all of
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
from typing import Any, cast

from elasticsearch import Elasticsearch
from fastapi import FastAPI
from opentelemetry import metrics as metrics
from opentelemetry import trace as trace
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor, RequestInfo
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Span

from curator.settings import Settings

SERVICE_NAME_VALUE = "crgolden-curator"
_HEALTH_EXCLUDED_URLS = "health"

_METRIC_EXPORT_INTERVAL_MILLIS = 15_000

_REDACT_QUERY_PARAM_HOSTS = ("api.rawg.io",)
_REDACT_QUERY_PARAM_NAME = "key"
_URL_SPAN_ATTRIBUTE_KEYS = ("http.url", "url.full")


async def _redact_rawg_key_from_span(span: Span, request_info: RequestInfo) -> None:
    """Strip the RAWG ``key`` query parameter from a span's URL attribute right after the httpx
    instrumentor sets it, so a user's own RAWG API key never lands in a Tempo trace.
    """
    if not span.is_recording() or request_info.url.host not in _REDACT_QUERY_PARAM_HOSTS:
        return
    sanitized = request_info.url.copy_with(
        params={key: value for key, value in request_info.url.params.items() if key != _REDACT_QUERY_PARAM_NAME}
    )
    existing_attributes = getattr(span, "attributes", None) or {}
    for attribute_key in _URL_SPAN_ATTRIBUTE_KEYS:
        if attribute_key in existing_attributes:
            span.set_attribute(attribute_key, str(sanitized))


_ES_DATA_STREAM = "logs-app-curator"

_LEVEL_NAMES = {
    "DEBUG": "Debug",
    "INFO": "Information",
    "WARNING": "Warning",
    "ERROR": "Error",
    "CRITICAL": "Fatal",
}

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
    try:
        _configure_tracing_and_metrics(app, settings)
    except Exception as exc:
        print(f"curator.telemetry: OTLP telemetry setup failed, continuing without it: {exc}", file=sys.stderr)

    try:
        _configure_elasticsearch_logging(settings)
    except Exception as exc:
        print(f"curator.telemetry: Elasticsearch logging setup failed, continuing without it: {exc}", file=sys.stderr)


def _configure_tracing_and_metrics(app: FastAPI, settings: Settings) -> None:
    """Configure OTLP traces + metrics and instrument FastAPI/psycopg/httpx, iff ``alloy_endpoint`` is set.

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
    calls, as in tests) never stack a second provider or double-instrument psycopg/httpx.

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
            metric_readers=[
                PeriodicExportingMetricReader(
                    OTLPMetricExporter(endpoint=alloy_endpoint),
                    export_interval_millis=_METRIC_EXPORT_INTERVAL_MILLIS,
                )
            ],
        )
        metrics.set_meter_provider(meter_provider)

        psycopg_instrumentor = PsycopgInstrumentor()
        if not psycopg_instrumentor.is_instrumented_by_opentelemetry:
            psycopg_instrumentor.instrument()

        httpx_instrumentor = HTTPXClientInstrumentor()
        if not httpx_instrumentor.is_instrumented_by_opentelemetry:
            httpx_instrumentor.instrument(async_request_hook=_redact_rawg_key_from_span)

        _otel_configured = True


def shutdown_telemetry() -> None:
    """Flush and shut down the tracer/meter providers, iff OTLP telemetry was configured.

    Call once from the app's lifespan shutdown. Spans already flush continuously via
    ``BatchSpanProcessor``'s own background thread, so this matters most for metrics --
    ``PeriodicExportingMetricReader`` only leaves the process on its timer or an explicit
    ``shutdown()``/``force_flush()``, and a gunicorn worker recycled between timer ticks would
    otherwise drop whatever it had accumulated. Swallows any failure, matching every other
    telemetry entry point in this module: a slow or unreachable collector at shutdown must never
    turn a clean exit into a crash or a hung process.
    """
    if not _otel_configured:
        return
    with contextlib.suppress(Exception):
        cast(TracerProvider, trace.get_tracer_provider()).shutdown()
    with contextlib.suppress(Exception):
        cast(MeterProvider, metrics.get_meter_provider()).shutdown()


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

        logging.getLogger("elastic_transport").propagate = False
        logging.getLogger("elasticsearch").propagate = False

        log_queue: SimpleQueue[logging.LogRecord] = SimpleQueue()
        queue_handler = QueueHandler(log_queue)
        queue_handler.setLevel(logging.WARNING)
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
    ``log.level`` is translated from Python's own level names to the fleet's Serilog/ECS vocabulary (see
    :data:`_LEVEL_NAMES`) so it matches every other app's spelling; an unrecognized level name (there
    should never be one) falls back to Python's own name rather than raising.

    :param record: The log record to format.
    :returns: A JSON-serializable document.
    """
    doc: dict[str, Any] = {
        "@timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
        "message": record.getMessage(),
        "log.level": _LEVEL_NAMES.get(record.levelname, record.levelname),
        "service.name": SERVICE_NAME_VALUE,
        "logger.name": record.name,
    }
    if record.exc_info:
        doc["error.stack_trace"] = logging.Formatter().formatException(record.exc_info)
    return doc


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
        """Index one formatted log document into the ``logs-app-curator`` data stream, swallowing any
        failure.

        Data streams are append-only: writes must use ``op_type="create"`` (the default ``"index"`` op
        type is rejected once the target is an actual data stream, not a bare index). No document id is
        supplied -- Elasticsearch auto-generates one, exactly like the ``create`` op type expects.
        ``require_data_stream=True`` fails the write loudly (swallowed by the ``suppress`` below, same as
        any other transient failure) if ``_ES_DATA_STREAM`` were ever misconfigured into resolving to a
        bare index instead of a real data stream, rather than silently succeeding against the wrong kind
        of target the way the previous day-bucketed bare index did.

        :param record: The log record to ship.
        """
        with contextlib.suppress(Exception):
            self._client.index(
                index=_ES_DATA_STREAM,
                document=format_log_record(record),
                op_type="create",
                require_data_stream=True,
            )


_QUEUE_DEPTH_METER = metrics.get_meter(__name__)
_QUEUE_ACTIVE_COUNTS: dict[str, int] = {}
_QUEUE_DEAD_LETTER_COUNTS: dict[str, int] = {}


def _observe_queue_active(_options: CallbackOptions) -> list[Observation]:
    return [Observation(count, {"queue": queue}) for queue, count in _QUEUE_ACTIVE_COUNTS.items()]


def _observe_queue_dead_letter(_options: CallbackOptions) -> list[Observation]:
    return [Observation(count, {"queue": queue}) for queue, count in _QUEUE_DEAD_LETTER_COUNTS.items()]


_QUEUE_DEPTH_METER.create_observable_gauge(
    "curator.servicebus.queue.active",
    callbacks=[_observe_queue_active],
    description="Active message count per Service Bus queue, refreshed by QueueDepthMonitor.",
)
_QUEUE_DEPTH_METER.create_observable_gauge(
    "curator.servicebus.queue.deadletter",
    callbacks=[_observe_queue_dead_letter],
    description="Dead-lettered message count per Service Bus queue, refreshed by QueueDepthMonitor.",
)


def record_queue_depth(queue: str, active_message_count: int, dead_letter_message_count: int) -> None:
    """Records the latest active/dead-lettered message counts for a Service Bus queue.

    :param queue: The queue name.
    :param active_message_count: The queue's current active message count.
    :param dead_letter_message_count: The queue's current dead-lettered message count.
    """
    _QUEUE_ACTIVE_COUNTS[queue] = active_message_count
    _QUEUE_DEAD_LETTER_COUNTS[queue] = dead_letter_message_count
