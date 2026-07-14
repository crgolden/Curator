"""Tests for QueueConsumer, using hand-written fake Service Bus receiver + job-runs repository (no real
Azure connection, no real database)."""

from __future__ import annotations

from curator.jobs.queue_consumer import QueueConsumer


class FakeMessage:
    def __init__(self, body):
        self._body = body

    def __str__(self):
        return self._body


class FakeReceiver:
    """Stands in for an async Service Bus receiver: iterates a fixed list of messages once, then stops."""

    def __init__(self, messages):
        self._messages = list(messages)
        self.completed = []
        self.dead_lettered = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def complete_message(self, message):
        self.completed.append(message)

    async def dead_letter_message(self, message, *, reason, error_description=None):
        self.dead_lettered.append((message, reason, error_description))


class RecordingHandler:
    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises

    async def __call__(self, *args):
        self.calls.append(args)
        if self._raises:
            raise self._raises


class FakeJobRunsRepository:
    def __init__(self):
        self.running: list[str] = []
        self.succeeded: list[str] = []
        self.failed: list[tuple[str, str]] = []

    async def mark_running(self, run_id):
        self.running.append(run_id)

    async def mark_succeeded(self, run_id):
        self.succeeded.append(run_id)

    async def mark_failed(self, run_id, error):
        self.failed.append((run_id, error))


def _consumer(
    library_messages=(),
    enrichment_messages=(),
    on_library_refresh=None,
    on_enrichment_run=None,
    job_runs_repository=None,
):
    return QueueConsumer(
        library_refresh_receiver=FakeReceiver(library_messages),
        enrichment_receiver=FakeReceiver(enrichment_messages),
        on_library_refresh=on_library_refresh or RecordingHandler(),
        on_enrichment_run=on_enrichment_run or RecordingHandler(),
        job_runs_repository=job_runs_repository or FakeJobRunsRepository(),
    )


async def test_library_refresh_dispatches_identity_sub_and_completes():
    handler = RecordingHandler()
    receiver = FakeReceiver([FakeMessage('{"run_id": "r1", "identity_sub": "sub-1"}')])
    job_runs_repository = FakeJobRunsRepository()
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh()

    assert handler.calls == [("sub-1",)]
    assert len(receiver.completed) == 1
    assert receiver.dead_lettered == []
    assert job_runs_repository.running == ["r1"]
    assert job_runs_repository.succeeded == ["r1"]


async def test_enrichment_run_dispatches_with_no_args_and_completes():
    handler = RecordingHandler()
    receiver = FakeReceiver([FakeMessage('{"run_id": "r1"}')])
    consumer = QueueConsumer(
        library_refresh_receiver=FakeReceiver([]),
        enrichment_receiver=receiver,
        on_library_refresh=RecordingHandler(),
        on_enrichment_run=handler,
        job_runs_repository=FakeJobRunsRepository(),
    )

    await consumer.drain_enrichment()

    assert handler.calls == [()]
    assert len(receiver.completed) == 1


async def test_invalid_json_dead_letters_as_malformed_payload():
    receiver = FakeReceiver([FakeMessage("not json")])
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=FakeJobRunsRepository(),
    )

    await consumer.drain_library_refresh()

    assert receiver.completed == []
    assert len(receiver.dead_lettered) == 1
    _, reason, _description = receiver.dead_lettered[0]
    assert reason == "malformed-payload"


async def test_non_object_json_dead_letters_as_malformed_payload():
    receiver = FakeReceiver([FakeMessage("[1, 2, 3]")])
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=FakeJobRunsRepository(),
    )

    await consumer.drain_library_refresh()

    assert receiver.dead_lettered[0][1] == "malformed-payload"


async def test_missing_run_id_dead_letters_as_malformed_payload():
    handler = RecordingHandler()
    receiver = FakeReceiver([FakeMessage('{"identity_sub": "sub-1"}')])
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=FakeJobRunsRepository(),
    )

    await consumer.drain_library_refresh()

    assert handler.calls == []
    assert receiver.dead_lettered[0][1] == "malformed-payload"
    assert "run_id" in receiver.dead_lettered[0][2]


async def test_missing_identity_sub_dead_letters_as_malformed_payload():
    handler = RecordingHandler()
    receiver = FakeReceiver([FakeMessage('{"run_id": "r1"}')])
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=FakeJobRunsRepository(),
    )

    await consumer.drain_library_refresh()

    assert handler.calls == []
    assert receiver.dead_lettered[0][1] == "malformed-payload"
    assert "identity_sub" in receiver.dead_lettered[0][2]


async def test_processing_exception_dead_letters_as_processing_failed_and_marks_run_failed():
    handler = RecordingHandler(raises=RuntimeError("boom"))
    receiver = FakeReceiver([FakeMessage('{"run_id": "r1", "identity_sub": "sub-1"}')])
    job_runs_repository = FakeJobRunsRepository()
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh()

    assert receiver.completed == []
    reason, description = receiver.dead_lettered[0][1], receiver.dead_lettered[0][2]
    assert reason == "processing-failed"
    assert "boom" in description
    assert job_runs_repository.failed == [("r1", "boom")]
    assert job_runs_repository.succeeded == []


async def test_multiple_messages_processed_independently():
    handler = RecordingHandler()
    messages = [
        FakeMessage('{"run_id": "r1", "identity_sub": "sub-1"}'),
        FakeMessage("not json"),
        FakeMessage('{"run_id": "r2", "identity_sub": "sub-2"}'),
    ]
    receiver = FakeReceiver(messages)
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=FakeJobRunsRepository(),
    )

    await consumer.drain_library_refresh()

    assert handler.calls == [("sub-1",), ("sub-2",)]
    assert len(receiver.completed) == 2
    assert len(receiver.dead_lettered) == 1


async def test_start_and_stop_manage_background_tasks():
    consumer = _consumer()

    consumer.start()
    assert len(consumer._tasks) == 2

    await consumer.stop()
    assert consumer._tasks == []
