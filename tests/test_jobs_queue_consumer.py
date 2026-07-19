"""Tests for QueueConsumer, using hand-written fake Service Bus receiver + job-runs repository (no real
Azure connection, no real database)."""

from __future__ import annotations

from typing import Any

from curator.jobs.queue_consumer import QueueConsumer


class FakeMessage:
    def __init__(self, body):
        self._body = body

    def __str__(self):
        return self._body


class FakeReceiver:
    """Stands in for an async Service Bus receiver: iterates a fixed list of messages once, then stops."""

    def __init__(self, messages, *, dead_letter_raises=None, complete_raises=None):
        self._messages = list(messages)
        self.completed = []
        self.dead_lettered = []
        self._dead_letter_raises = dead_letter_raises
        self._complete_raises = complete_raises

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def complete_message(self, message):
        if self._complete_raises:
            raise self._complete_raises
        self.completed.append(message)

    async def dead_letter_message(self, message, *, reason, error_description=None):
        if self._dead_letter_raises:
            raise self._dead_letter_raises
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
        self.succeeded_summaries: dict[str, dict | None] = {}
        self.failed: list[tuple[str, str]] = []

    async def mark_running(self, run_id):
        self.running.append(run_id)

    async def mark_succeeded(self, run_id, result_summary=None):
        self.succeeded.append(run_id)
        self.succeeded_summaries[run_id] = result_summary

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
    assert "boom" not in description  # raw exception text never reaches here -- see curator.jobs.error_messages
    assert job_runs_repository.failed == [
        ("r1", "The job failed unexpectedly. If this keeps happening, contact support.")
    ]
    assert job_runs_repository.succeeded == []


async def test_processing_exception_uses_friendly_message_for_known_exception_types():
    from curator.enrichment.enrichment_service import EnrichmentAuthError

    handler = RecordingHandler(raises=EnrichmentAuthError("rawg", "RAWG request failed with status 401"))
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

    assert job_runs_repository.failed == [
        ("r1", "Your RAWG API key was rejected. Check that it's correct and try again.")
    ]
    assert receiver.dead_lettered[0][2] == "Your RAWG API key was rejected. Check that it's correct and try again."


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


async def test_dead_letter_message_failure_does_not_stop_the_drain_loop():
    """Reproduces the production incident: a processing failure's own ``dead_letter_message`` call raises
    (e.g. a transient Service Bus error) -- the drain loop must survive and still process the next
    message, not silently die and leave every future message stuck at ``queued`` forever."""
    handler = RecordingHandler(raises=RuntimeError("boom"))
    messages = [
        FakeMessage('{"run_id": "r1", "identity_sub": "sub-1"}'),
        FakeMessage('{"run_id": "r2", "identity_sub": "sub-2"}'),
    ]
    receiver = FakeReceiver(messages, dead_letter_raises=RuntimeError("service bus unavailable"))
    job_runs_repository = FakeJobRunsRepository()
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh()

    assert handler.calls == [("sub-1",), ("sub-2",)]
    friendly = "The job failed unexpectedly. If this keeps happening, contact support."
    assert job_runs_repository.failed == [("r1", friendly), ("r2", friendly)]


async def test_complete_message_failure_does_not_stop_the_drain_loop():
    """Same failure mode as above, but on the success path: ``complete_message`` itself raising after a
    job finished successfully must not kill the loop either."""
    handler = RecordingHandler()
    messages = [
        FakeMessage('{"run_id": "r1", "identity_sub": "sub-1"}'),
        FakeMessage('{"run_id": "r2", "identity_sub": "sub-2"}'),
    ]
    receiver = FakeReceiver(messages, complete_raises=RuntimeError("service bus unavailable"))
    job_runs_repository = FakeJobRunsRepository()
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh()

    assert handler.calls == [("sub-1",), ("sub-2",)]
    assert job_runs_repository.succeeded == ["r1", "r2"]


class FakeLockRenewer:
    def __init__(self) -> None:
        self.registered: list[tuple[Any, Any]] = []
        self.closed = False

    def register(self, receiver, message):
        self.registered.append((receiver, message))

    async def close(self):
        self.closed = True


async def test_default_lock_renewer_is_a_noop():
    """No lock_renewer passed -- every existing test in this file relies on this being safe."""
    handler = RecordingHandler()
    receiver = FakeReceiver([FakeMessage('{"run_id": "r1", "identity_sub": "sub-1"}')])
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=FakeJobRunsRepository(),
    )

    await consumer.drain_library_refresh()  # must not raise

    assert handler.calls == [("sub-1",)]
    assert len(receiver.completed) == 1


async def test_lock_renewer_registered_once_per_message():
    lock_renewer = FakeLockRenewer()
    messages = [
        FakeMessage('{"run_id": "r1", "identity_sub": "sub-1"}'),
        FakeMessage('{"run_id": "r2", "identity_sub": "sub-2"}'),
    ]
    receiver = FakeReceiver(messages)
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=FakeJobRunsRepository(),
        lock_renewer=lock_renewer,
    )

    await consumer.drain_library_refresh()

    assert len(lock_renewer.registered) == 2
    assert lock_renewer.registered[0] == (receiver, messages[0])
    assert lock_renewer.registered[1] == (receiver, messages[1])


async def test_start_and_stop_manage_background_tasks():
    consumer = _consumer()

    consumer.start()
    assert len(consumer._tasks) == 2

    await consumer.stop()
    assert consumer._tasks == []
