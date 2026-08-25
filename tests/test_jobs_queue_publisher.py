"""Tests for QueuePublisher, using hand-written fake Service Bus sender + job-runs repository (no real
Azure connection, no real database)."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime

import pytest
from azure.servicebus import ServiceBusMessage
from opentelemetry.sdk.trace import TracerProvider

from curator.jobs.queue_publisher import QueuePublisher

_TRACE_PARENT_PATTERN = r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"


class FakeSender:
    def __init__(self):
        self.sent: list[ServiceBusMessage] = []
        self.scheduled: list[tuple[ServiceBusMessage, datetime]] = []

    async def send_messages(self, message):
        self.sent.append(message)

    async def schedule_messages(self, message, schedule_time_utc):
        self.scheduled.append((message, schedule_time_utc))
        return [1]


class FakeJobRunsRepository:
    def __init__(self):
        self.created: list[tuple] = []

    async def create(self, run_id, kind, identity_sub=None):
        self.created.append((run_id, kind, identity_sub))


def _make_publisher(library_sender=None, enrichment_sender=None, job_runs_repository=None):
    return QueuePublisher(
        library_refresh_sender=library_sender or FakeSender(),
        enrichment_sender=enrichment_sender or FakeSender(),
        job_runs_repository=job_runs_repository or FakeJobRunsRepository(),
    )


async def test_publish_library_refresh_sends_identity_sub_and_returns_run_id():
    library_sender = FakeSender()
    job_runs_repository = FakeJobRunsRepository()
    publisher = _make_publisher(library_sender=library_sender, job_runs_repository=job_runs_repository)

    run_id = await publisher.publish_library_refresh("sub-1")

    assert uuid.UUID(run_id)
    assert len(library_sender.sent) == 1
    body = json.loads(str(library_sender.sent[0]))
    assert body == {"run_id": run_id, "identity_sub": "sub-1"}
    assert job_runs_repository.created == [(run_id, "library_refresh", "sub-1")]


async def test_publish_enrichment_run_sends_to_enrichment_sender_only():
    library_sender = FakeSender()
    enrichment_sender = FakeSender()
    job_runs_repository = FakeJobRunsRepository()
    publisher = _make_publisher(
        library_sender=library_sender, enrichment_sender=enrichment_sender, job_runs_repository=job_runs_repository
    )

    run_id = await publisher.publish_enrichment_run()

    assert uuid.UUID(run_id)
    assert library_sender.sent == []
    assert len(enrichment_sender.sent) == 1
    body = json.loads(str(enrichment_sender.sent[0]))
    assert body == {"run_id": run_id}
    assert job_runs_repository.created == [(run_id, "enrichment", None)]


async def test_each_publish_generates_a_distinct_run_id():
    publisher = _make_publisher()

    run_id_1 = await publisher.publish_library_refresh("sub-1")
    run_id_2 = await publisher.publish_library_refresh("sub-1")

    assert run_id_1 != run_id_2


@pytest.fixture
def recording_tracer():
    """A tracer from its own TracerProvider, never the global one.

    ``inject`` reads the *active span* from the current context, not the global provider, so a local
    provider is enough -- and registering one globally would fail anyway. Whichever test happens to run
    first in a worker wins ``set_tracer_provider``; every later call logs "Overriding of current
    TracerProvider is not allowed" and leaves a ``ProxyTracer`` whose spans have no span context, so the
    assertions here would fail for a reason that has nothing to do with the code under test.
    """
    return TracerProvider().get_tracer("test_jobs_queue_publisher")


def _property(message: ServiceBusMessage, name: str) -> str:
    properties = message.application_properties
    assert properties is not None
    return str(properties[name])


async def test_library_refresh_message_carries_the_calling_span_trace_id(recording_tracer):
    library_sender = FakeSender()
    publisher = _make_publisher(library_sender=library_sender)

    with recording_tracer.start_as_current_span("POST /library/refresh") as span:
        await publisher.publish_library_refresh("sub-1")
        expected_trace_id = format(span.get_span_context().trace_id, "032x")

    assert expected_trace_id in _property(library_sender.sent[0], "traceparent")


async def test_library_refresh_message_carries_both_property_names_the_worker_reads(recording_tracer):
    library_sender = FakeSender()
    publisher = _make_publisher(library_sender=library_sender)

    with recording_tracer.start_as_current_span("POST /library/refresh"):
        await publisher.publish_library_refresh("sub-1")

    message = library_sender.sent[0]
    assert _property(message, "Diagnostic-Id") == _property(message, "traceparent")
    assert re.match(_TRACE_PARENT_PATTERN, _property(message, "traceparent"))


async def test_enrichment_message_carries_trace_context_too(recording_tracer):
    enrichment_sender = FakeSender()
    publisher = _make_publisher(enrichment_sender=enrichment_sender)

    with recording_tracer.start_as_current_span("POST /enrichment/run"):
        await publisher.publish_enrichment_run()

    assert re.match(_TRACE_PARENT_PATTERN, _property(enrichment_sender.sent[0], "traceparent"))


async def test_publish_outside_a_span_sends_a_message_with_no_trace_properties():
    library_sender = FakeSender()
    publisher = _make_publisher(library_sender=library_sender)

    await publisher.publish_library_refresh("sub-1")

    assert not library_sender.sent[0].application_properties


async def test_scheduled_refresh_carries_no_trace_context(recording_tracer):
    scheduled_sender = FakeSender()
    publisher = QueuePublisher(
        library_refresh_sender=FakeSender(),
        enrichment_sender=FakeSender(),
        job_runs_repository=FakeJobRunsRepository(),
        scheduled_refresh_sender=scheduled_sender,
    )

    with recording_tracer.start_as_current_span("PUT /library/refresh/schedule"):
        await publisher.publish_scheduled_library_refresh("sub-1", datetime(2026, 9, 1, 12, 0, 0))

    message, _ = scheduled_sender.scheduled[0]
    assert not message.application_properties
