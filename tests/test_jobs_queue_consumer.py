"""Tests for QueueConsumer, using hand-written fake Service Bus receiver + job-runs repository (no real
Azure connection, no real database)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from azure.servicebus.exceptions import MessageLockLostError, ServiceBusConnectionError

from curator.jobs import queue_consumer as queue_consumer_module
from curator.jobs.queue_consumer import QueueConsumer, RateLimitRetryScheduled


class FakeMessage:
    def __init__(self, body):
        self._body = body

    def __str__(self):
        return self._body


class FakeReceiver:
    """Stands in for an async Service Bus receiver: iterates a fixed list of messages once, then stops.

    ``dead_letter_raises``/``complete_raises`` accept either a single exception (raised on every call --
    what every pre-existing test in this file uses) or a list of "next exception to raise, or ``None`` for
    a call that should succeed" consumed in call order (for exercising a settle call that fails once then
    succeeds, or that raises a lock-loss error tenacity must not retry).
    """

    def __init__(self, messages, *, dead_letter_raises=None, complete_raises=None, iterate_raises=None):
        self._messages = list(messages)
        self.completed = []
        self.dead_lettered = []
        self._dead_letter_raises = dead_letter_raises
        self._complete_raises = complete_raises
        self._iterate_raises = list(iterate_raises) if iterate_raises is not None else None
        self.complete_call_count = 0
        self.dead_letter_call_count = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._iterate_raises:
            exc = self._iterate_raises.pop(0)
            if exc is not None:
                raise exc
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def complete_message(self, message):
        self.complete_call_count += 1
        exc = self._next_exception(self._complete_raises)
        if exc is not None:
            raise exc
        self.completed.append(message)

    async def dead_letter_message(self, message, *, reason, error_description=None):
        self.dead_letter_call_count += 1
        exc = self._next_exception(self._dead_letter_raises)
        if exc is not None:
            raise exc
        self.dead_lettered.append((message, reason, error_description))

    @staticmethod
    def _next_exception(raises):
        if raises is None:
            return None
        if isinstance(raises, list):
            return raises.pop(0) if raises else None
        return raises


class RecordingHandler:
    def __init__(self, raises=None):
        self.calls = []
        self._raises = raises

    async def __call__(self, *args):
        self.calls.append(args)
        if self._raises:
            raise self._raises


class FakeJobRunsRepository:
    """A real (if simplified) in-memory implementation of the compare-and-swap ``try_begin_delivery``
    performs against real Postgres, keyed by ``run_id`` -- not a stub that always returns ``True`` --
    since the whole point of these tests is exercising the guard itself."""

    def __init__(self, runs: dict[str, dict[str, Any]] | None = None):
        self._runs: dict[str, dict[str, Any]] = {run_id: dict(state) for run_id, state in (runs or {}).items()}
        self.began: list[str] = []
        self.succeeded: list[str] = []
        self.succeeded_summaries: dict[str, dict | None] = {}
        self.failed: list[tuple[str, str]] = []
        self.rate_limited_calls: list[tuple[str, dict]] = []
        self.lease_renewals: list[str] = []

    def seed(self, run_id: str, *, status: str = "queued", seq: int = 0, error: str | None = None) -> None:
        """Pre-populate a run's state before delivering a message, for tests exercising a stale
        redelivery (a seq already superseded, or a run already in a terminal status)."""
        self._runs[run_id] = {"status": status, "seq": seq, "error": error}

    def _run(self, run_id: str) -> dict[str, Any]:
        return self._runs.setdefault(run_id, {"status": "queued", "seq": 0, "error": None})

    async def try_begin_delivery(self, run_id, expected_seq, lease_seconds=None):
        run = self._run(run_id)
        if run["seq"] != expected_seq or run["status"] in ("succeeded", "failed"):
            return False
        if run["status"] == "running" and run.get("leased"):
            return False
        run["status"] = "running"
        run["leased"] = True
        self.began.append(run_id)
        return True

    async def renew_lease(self, run_id, lease_seconds=None):
        run = self._run(run_id)
        self.lease_renewals.append(run_id)
        if run["status"] != "running":
            return False
        run["leased"] = True
        return True

    async def get(self, run_id):
        """Satisfies ``_handle``'s ``get(run_id)`` lookup on a stale redelivery, to decide whether it's a
        silently-completable no-op (succeeded / superseded checkpoint) or an already-failed run that
        needs to dead-letter instead."""
        run = self._runs.get(run_id)
        if run is None:
            return None
        return SimpleNamespace(status=run["status"], error=run["error"])

    async def mark_succeeded(self, run_id, result_summary=None):
        self.succeeded.append(run_id)
        self.succeeded_summaries[run_id] = result_summary
        run = self._run(run_id)
        run["status"] = "succeeded"
        run["leased"] = False

    async def mark_failed(self, run_id, error):
        self.failed.append((run_id, error))
        run = self._run(run_id)
        run["status"] = "failed"
        run["error"] = error
        run["leased"] = False

    async def mark_rate_limited(self, run_id, result_summary):
        self.rate_limited_calls.append((run_id, result_summary))
        run = self._run(run_id)
        run["seq"] += 1
        run["status"] = "rate_limited"
        run["leased"] = False
        return run["seq"]


def _consumer(
    library_messages=(),
    continuation_messages=(),
    enrichment_messages=(),
    on_library_refresh=None,
    on_library_refresh_continuation=None,
    on_enrichment_run=None,
    job_runs_repository=None,
):
    return QueueConsumer(
        library_refresh_receiver=FakeReceiver(library_messages),
        library_refresh_continuation_receiver=FakeReceiver(continuation_messages),
        enrichment_receiver=FakeReceiver(enrichment_messages),
        on_library_refresh=on_library_refresh or RecordingHandler(),
        on_library_refresh_continuation=on_library_refresh_continuation or RecordingHandler(),
        on_enrichment_run=on_enrichment_run or RecordingHandler(),
        job_runs_repository=job_runs_repository or FakeJobRunsRepository(),
    )


async def test_library_refresh_dispatches_identity_sub_and_completes():
    handler = RecordingHandler()
    receiver = FakeReceiver([FakeMessage('{"run_id": "r1", "identity_sub": "sub-1"}')])
    job_runs_repository = FakeJobRunsRepository()
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_library_refresh_continuation=RecordingHandler(),
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh()

    assert handler.calls == [("r1", "sub-1")]
    assert len(receiver.completed) == 1
    assert receiver.dead_lettered == []
    assert job_runs_repository.began == ["r1"]
    assert job_runs_repository.succeeded == ["r1"]


async def test_enrichment_run_dispatches_with_no_args_and_completes():
    handler = RecordingHandler()
    receiver = FakeReceiver([FakeMessage('{"run_id": "r1"}')])
    consumer = QueueConsumer(
        library_refresh_receiver=FakeReceiver([]),
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=receiver,
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=RecordingHandler(),
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
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=RecordingHandler(),
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
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=RecordingHandler(),
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
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_library_refresh_continuation=RecordingHandler(),
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
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_library_refresh_continuation=RecordingHandler(),
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
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_library_refresh_continuation=RecordingHandler(),
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
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_library_refresh_continuation=RecordingHandler(),
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
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_library_refresh_continuation=RecordingHandler(),
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=FakeJobRunsRepository(),
    )

    await consumer.drain_library_refresh()

    assert handler.calls == [("r1", "sub-1"), ("r2", "sub-2")]
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
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_library_refresh_continuation=RecordingHandler(),
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh()

    assert handler.calls == [("r1", "sub-1"), ("r2", "sub-2")]
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
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_library_refresh_continuation=RecordingHandler(),
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh()

    assert handler.calls == [("r1", "sub-1"), ("r2", "sub-2")]
    assert job_runs_repository.succeeded == ["r1", "r2"]


async def test_receiver_iteration_failure_reconnects_and_keeps_draining(monkeypatch):
    """The ``async for`` iteration itself -- not per-message handling -- can raise: e.g. a Service Bus
    connectivity blip while fetching the next message, distinct from every failure exercised above (which
    all happen *after* a message was already received). This used to propagate out of the drain method
    entirely, killing its background task with no supervisor to restart it and silencing the queue until
    the whole app restarted. Now it must be caught, logged, and followed by a backoff before reconnecting
    on the same receiver -- proven here by a message becoming processable only after the reconnect."""
    sleeps: list[float] = []
    monkeypatch.setattr("asyncio.sleep", _record_sleep(sleeps))
    handler = RecordingHandler()
    receiver = FakeReceiver(
        [FakeMessage('{"run_id": "r1", "identity_sub": "sub-1"}')],
        iterate_raises=[ServiceBusConnectionError()],
    )
    job_runs_repository = FakeJobRunsRepository()
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_library_refresh_continuation=RecordingHandler(),
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh()  # must not raise

    assert handler.calls == [("r1", "sub-1")]  # processed only after the reconnect
    assert job_runs_repository.succeeded == ["r1"]
    assert sleeps == [queue_consumer_module._RECEIVE_ERROR_BACKOFF_SECONDS]


async def test_receiver_iteration_cancellation_still_propagates(monkeypatch):
    """Unlike a real receive error, cancellation (from :meth:`QueueConsumer.stop`) must never be treated
    as a reconnect-worthy failure -- it has to propagate so ``stop()``'s ``await task`` actually completes."""
    sleeps: list[float] = []
    monkeypatch.setattr("asyncio.sleep", _record_sleep(sleeps))
    receiver = FakeReceiver([], iterate_raises=[asyncio.CancelledError()])
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=RecordingHandler(),
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=FakeJobRunsRepository(),
    )

    with pytest.raises(asyncio.CancelledError):
        await consumer.drain_library_refresh()

    assert sleeps == []


def _record_sleep(sleeps: list[float]):
    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return _sleep


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
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_library_refresh_continuation=RecordingHandler(),
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=FakeJobRunsRepository(),
    )

    await consumer.drain_library_refresh()  # must not raise

    assert handler.calls == [("r1", "sub-1")]
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
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=RecordingHandler(),
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
    assert len(consumer._tasks) == 3

    await consumer.stop()
    assert consumer._tasks == []


_CONTINUATION_PAYLOAD = (
    '{"run_id": "r1", "identity_sub": "sub-1", "remaining_game_ids": ["g1", "g2"], '
    '"provider": "rawg", "retry_after_seconds": 3600}'
)


async def test_library_refresh_continuation_dispatches_full_payload_and_completes():
    handler = RecordingHandler()
    receiver = FakeReceiver([FakeMessage(_CONTINUATION_PAYLOAD)])
    job_runs_repository = FakeJobRunsRepository()
    consumer = QueueConsumer(
        library_refresh_receiver=FakeReceiver([]),
        library_refresh_continuation_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh_continuation()

    assert handler.calls == [
        (
            {
                "run_id": "r1",
                "identity_sub": "sub-1",
                "remaining_game_ids": ["g1", "g2"],
                "provider": "rawg",
                "retry_after_seconds": 3600,
            },
        )
    ]
    assert len(receiver.completed) == 1
    assert job_runs_repository.began == ["r1"]
    assert job_runs_repository.succeeded == ["r1"]


async def test_library_refresh_continuation_missing_field_dead_letters_as_malformed_payload():
    receiver = FakeReceiver([FakeMessage('{"run_id": "r1", "identity_sub": "sub-1"}')])
    consumer = QueueConsumer(
        library_refresh_receiver=FakeReceiver([]),
        library_refresh_continuation_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=RecordingHandler(),
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=FakeJobRunsRepository(),
    )

    await consumer.drain_library_refresh_continuation()

    assert receiver.completed == []
    assert receiver.dead_lettered[0][1] == "malformed-payload"
    assert "remaining_game_ids" in receiver.dead_lettered[0][2]


async def test_rate_limit_retry_scheduled_completes_message_without_status_write_or_dead_letter():
    """The handler already ran mark_rate_limited + republished itself before raising -- the consumer must
    not also mark_succeeded/mark_failed or dead-letter."""
    handler = RecordingHandler(raises=RateLimitRetryScheduled())
    receiver = FakeReceiver([FakeMessage(_CONTINUATION_PAYLOAD)])
    job_runs_repository = FakeJobRunsRepository()
    consumer = QueueConsumer(
        library_refresh_receiver=FakeReceiver([]),
        library_refresh_continuation_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh_continuation()

    assert len(receiver.completed) == 1
    assert receiver.dead_lettered == []
    assert job_runs_repository.succeeded == []
    assert job_runs_repository.failed == []


async def test_lock_loss_completing_a_message_is_swallowed_not_propagated():
    """Reproduces the actual production bug this WP fixes: ``complete_message`` raising
    ``MessageLockLostError`` inside the ``except RateLimitRetryScheduled`` branch used to propagate all
    the way out of ``_handle``, get logged-and-swallowed by the *drain loop's* own broad ``except``, and
    leave the message unsettled at the broker -- which is what let Service Bus redeliver it and reprocess
    the whole batch from scratch. Now ``_settle`` itself swallows the lock loss: no exception escapes
    ``_handle`` at all, this is never treated as a processing failure (``mark_failed`` is never called --
    it wasn't a failure, the handler already did its own bookkeeping before raising
    ``RateLimitRetryScheduled``), and the drain loop naturally continues to the next message."""
    handler = RecordingHandler(raises=RateLimitRetryScheduled())
    second_payload = _CONTINUATION_PAYLOAD.replace('"run_id": "r1"', '"run_id": "r2"')
    messages = [FakeMessage(_CONTINUATION_PAYLOAD), FakeMessage(second_payload)]
    receiver = FakeReceiver(messages, complete_raises=MessageLockLostError())
    job_runs_repository = FakeJobRunsRepository()
    consumer = QueueConsumer(
        library_refresh_receiver=FakeReceiver([]),
        library_refresh_continuation_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh_continuation()  # must not raise

    assert len(handler.calls) == 2  # both messages were processed -- the loop kept draining
    assert receiver.completed == []  # the lock was lost on every attempt, so nothing was ever appended
    assert receiver.complete_call_count == 2  # one settle attempt per message, no retries (not transient)
    assert job_runs_repository.failed == []
    assert job_runs_repository.succeeded == []


async def test_transient_service_bus_error_completing_a_message_retries_and_succeeds():
    """A real connectivity blip -- unlike a lock loss -- is worth retrying, since the exact same call can
    succeed a moment later. ``_settle_with_retry``'s tenacity wrapper is what makes that retry happen."""
    handler = RecordingHandler()
    receiver = FakeReceiver(
        [FakeMessage('{"run_id": "r1", "identity_sub": "sub-1"}')],
        complete_raises=[ServiceBusConnectionError(), None],
    )
    job_runs_repository = FakeJobRunsRepository()
    consumer = QueueConsumer(
        library_refresh_receiver=receiver,
        library_refresh_continuation_receiver=FakeReceiver([]),
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=handler,
        on_library_refresh_continuation=RecordingHandler(),
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh()  # must not raise

    assert receiver.complete_call_count == 2  # failed once, retried, succeeded on the second attempt
    assert len(receiver.completed) == 1
    assert job_runs_repository.succeeded == ["r1"]
    assert job_runs_repository.failed == []


async def test_stale_redelivery_with_superseded_seq_settles_without_reprocessing():
    """A redelivered continuation message whose ``seq`` no longer matches the run's current checkpoint
    (a later ``mark_rate_limited`` call has already superseded it) must not restart the batch -- this is
    the actual fix for the lock-loss/redelivery race: ``process()`` is never called, and the message is
    still settled so it doesn't sit around for yet another redelivery."""
    handler = RecordingHandler()
    job_runs_repository = FakeJobRunsRepository()
    job_runs_repository.seed("r1", status="running", seq=2)  # a later checkpoint already superseded seq 0
    receiver = FakeReceiver([FakeMessage(_CONTINUATION_PAYLOAD)])  # payload carries no "seq" field -> 0
    consumer = QueueConsumer(
        library_refresh_receiver=FakeReceiver([]),
        library_refresh_continuation_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh_continuation()

    assert handler.calls == []  # process() never called -- the whole point of the guard
    assert len(receiver.completed) == 1  # still settled, not left stuck for another redelivery
    assert receiver.dead_lettered == []
    assert job_runs_repository.began == []
    assert job_runs_repository.succeeded == []
    assert job_runs_repository.failed == []


async def test_concurrent_redelivery_of_the_current_checkpoint_is_refused_while_the_lease_is_live():
    """0026, the case the ``seq`` CAS alone could not catch. The checkpoint is still current (``seq`` 0
    matches) and the run is not terminal, so a seq-only guard would let this second copy through and both
    would burn real PSN/RAWG/OpenCritic budget concurrently. The live processing lease is what refuses it."""
    handler = RecordingHandler()
    job_runs_repository = FakeJobRunsRepository()
    job_runs_repository.seed("r1", status="running", seq=0)
    job_runs_repository._run("r1")["leased"] = True
    receiver = FakeReceiver([FakeMessage(_CONTINUATION_PAYLOAD)])
    consumer = QueueConsumer(
        library_refresh_receiver=FakeReceiver([]),
        library_refresh_continuation_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh_continuation()

    assert handler.calls == []  # no second concurrent copy of the work
    assert job_runs_repository.began == []
    assert len(receiver.completed) == 1  # settled, not left for yet another redelivery


async def test_redelivery_is_allowed_once_a_dead_processors_lease_has_lapsed():
    """The other half of the tradeoff: a run left at 'running' because its processor died must NOT be
    stuck forever. With the lease lapsed (``leased`` false), the redelivery claims it and reprocesses --
    which is exactly what excluding 'running' from the CAS outright would have broken."""
    handler = RecordingHandler()
    job_runs_repository = FakeJobRunsRepository()
    job_runs_repository.seed("r1", status="running", seq=0)  # no live lease -> processor is gone
    receiver = FakeReceiver([FakeMessage(_CONTINUATION_PAYLOAD)])
    consumer = QueueConsumer(
        library_refresh_receiver=FakeReceiver([]),
        library_refresh_continuation_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh_continuation()

    assert len(handler.calls) == 1  # recovered, not stuck
    assert job_runs_repository.began == ["r1"]
    assert job_runs_repository.succeeded == ["r1"]


async def test_stale_redelivery_of_an_already_succeeded_run_settles_without_reprocessing():
    """Same guard, the other benign trigger: the run already reached ``succeeded`` by the time this
    redelivery arrives (e.g. a second in-flight copy of the same message after the first was already
    processed to completion). Nothing went wrong here, so this stays a silent complete -- contrast with
    the already-``failed`` case below, which must dead-letter instead."""
    handler = RecordingHandler()
    job_runs_repository = FakeJobRunsRepository()
    job_runs_repository.seed("r1", status="succeeded", seq=0)
    receiver = FakeReceiver([FakeMessage(_CONTINUATION_PAYLOAD)])
    consumer = QueueConsumer(
        library_refresh_receiver=FakeReceiver([]),
        library_refresh_continuation_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh_continuation()

    assert handler.calls == []
    assert len(receiver.completed) == 1
    assert receiver.dead_lettered == []
    assert job_runs_repository.began == []


async def test_stale_redelivery_of_an_already_failed_run_dead_letters_with_stored_error():
    """The one case where a stale redelivery must NOT be silently completed: the run already failed --
    most likely because the original ``dead_letter_message`` attempt for it also failed and Service Bus
    redelivered the message. Silently completing here would make it vanish from both the active queue and
    the DLQ with zero operator visibility that anything ever failed. This must dead-letter instead, using
    the error already recorded on the run, and must still never reprocess (``process()`` is not called)."""
    handler = RecordingHandler()
    job_runs_repository = FakeJobRunsRepository()
    job_runs_repository.seed("r1", status="failed", seq=0, error="RAWG request failed with status 401")
    receiver = FakeReceiver([FakeMessage(_CONTINUATION_PAYLOAD)])
    consumer = QueueConsumer(
        library_refresh_receiver=FakeReceiver([]),
        library_refresh_continuation_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh_continuation()

    assert handler.calls == []  # no reprocessing
    assert receiver.completed == []
    assert len(receiver.dead_lettered) == 1
    _message, reason, description = receiver.dead_lettered[0]
    assert reason == "processing-failed"
    assert description == "RAWG request failed with status 401"
    assert job_runs_repository.began == []


async def test_stale_redelivery_of_an_already_failed_run_with_no_stored_error_uses_fallback_description():
    handler = RecordingHandler()
    job_runs_repository = FakeJobRunsRepository()
    job_runs_repository.seed("r1", status="failed", seq=0, error=None)
    receiver = FakeReceiver([FakeMessage(_CONTINUATION_PAYLOAD)])
    consumer = QueueConsumer(
        library_refresh_receiver=FakeReceiver([]),
        library_refresh_continuation_receiver=receiver,
        enrichment_receiver=FakeReceiver([]),
        on_library_refresh=RecordingHandler(),
        on_library_refresh_continuation=handler,
        on_enrichment_run=RecordingHandler(),
        job_runs_repository=job_runs_repository,
    )

    await consumer.drain_library_refresh_continuation()

    assert handler.calls == []
    assert len(receiver.dead_lettered) == 1
    _message, reason, description = receiver.dead_lettered[0]
    assert reason == "processing-failed"
    assert description == "run already failed"
