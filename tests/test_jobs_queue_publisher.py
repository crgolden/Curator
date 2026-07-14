"""Tests for QueuePublisher, using hand-written fake Service Bus sender + job-runs repository (no real
Azure connection, no real database)."""

from __future__ import annotations

import json
import uuid

from curator.jobs.queue_publisher import QueuePublisher


class FakeSender:
    def __init__(self):
        self.sent = []

    async def send_messages(self, message):
        self.sent.append(message)


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

    assert uuid.UUID(run_id)  # a real UUID was generated
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
