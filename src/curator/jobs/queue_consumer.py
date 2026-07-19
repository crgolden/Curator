"""Async consumer draining ``curator-library-refresh``/``curator-enrichment``, dispatching to
``curator.library.library_build_orchestrator``/``curator.enrichment.enrichment_service``.

Started in ``create_app()``'s lifespan, runs inside the same Curator process -- no second deployable
needed. Malformed messages (invalid JSON, missing required fields) and messages whose processing raises an
exception both dead-letter rather than being silently completed or retried forever, mirroring Functions'
``deadLetterReason: "malformed-payload"`` convention. Every message also carries a ``run_id`` (created in
``queued`` status by :class:`~curator.jobs.queue_publisher.QueuePublisher` before the message was even
sent); this consumer advances it to ``running`` before dispatching, then ``succeeded``/``failed`` after, via
the injected :class:`~curator.jobs.repository.JobRunsRepository` -- this is what
``GET /library/refresh/{run_id}`` reads.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from curator.jobs.error_messages import friendly_job_error
from curator.jobs.repository import JobRunsRepository

logger = logging.getLogger(__name__)

LibraryRefreshHandler = Callable[[str], Coroutine[Any, Any, dict[str, Any] | None]]
"""Processes one library-refresh job, given the target user's ``identity_sub``; returns an optional
result-summary payload for ``JobRunsRepository.mark_succeeded``."""

EnrichmentRunHandler = Callable[[], Coroutine[Any, Any, None]]
"""Processes one global enrichment-catalog re-scrape job."""


class MessageReceiver(Protocol):
    """Duck-typed async Service Bus receiver, satisfied by ``azure.servicebus.aio.ServiceBusReceiver``."""

    def __aiter__(self) -> Any:
        """Iterate received messages until the receiver is closed/cancelled."""
        ...

    async def complete_message(self, message: Any) -> None:
        """Acknowledge successful processing, removing the message from the queue."""
        ...

    async def dead_letter_message(self, message: Any, *, reason: str, error_description: str | None = None) -> None:
        """Move a message to the dead-letter sub-queue instead of completing or silently dropping it."""
        ...


class LockRenewer(Protocol):
    """Keeps a message's Service Bus lock alive for as long as it's being processed.

    Needed once per-user rate-limited enrichment (RAWG throttling, OpenCritic pagination top-ups) can make
    a single message's processing run longer than the queue's ``LockDuration`` (1 minute) -- without this,
    the lock would expire mid-processing and the message could be redelivered to a second receiver while
    still being handled by the first.
    """

    def register(self, receiver: Any, message: Any) -> None:
        """Start auto-renewing ``message``'s lock on ``receiver`` until it's completed/dead-lettered."""
        ...

    async def close(self) -> None:
        """Release any resources held for renewal (call once, from the app's lifespan shutdown)."""
        ...


class NullLockRenewer:
    """A no-op :class:`LockRenewer` -- the default, and what every existing fake-receiver-based test in
    this suite implicitly uses (no test passes ``lock_renewer=``)."""

    def register(self, receiver: Any, message: Any) -> None:
        return

    async def close(self) -> None:
        return


class QueueConsumer:
    """Drains both job queues, dispatching each message to its handler.

    :param library_refresh_receiver: A receiver bound to the ``curator-library-refresh`` queue.
    :param enrichment_receiver: A receiver bound to the ``curator-enrichment`` queue.
    :param on_library_refresh: Called with ``identity_sub`` for each library-refresh message.
    :param on_enrichment_run: Called for each enrichment-run message.
    :param job_runs_repository: Advances each message's ``run_id`` through ``running``/``succeeded``/
        ``failed`` around dispatch.
    :param lock_renewer: Keeps a long-running message's Service Bus lock alive; defaults to a
        :class:`NullLockRenewer` no-op (matches every existing test's implicit behavior).
    """

    def __init__(
        self,
        *,
        library_refresh_receiver: MessageReceiver,
        enrichment_receiver: MessageReceiver,
        on_library_refresh: LibraryRefreshHandler,
        on_enrichment_run: EnrichmentRunHandler,
        job_runs_repository: JobRunsRepository,
        lock_renewer: LockRenewer | None = None,
    ) -> None:
        self._library_refresh_receiver = library_refresh_receiver
        self._enrichment_receiver = enrichment_receiver
        self._on_library_refresh = on_library_refresh
        self._on_enrichment_run = on_enrichment_run
        self._job_runs_repository = job_runs_repository
        self._lock_renewer = lock_renewer or NullLockRenewer()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        """Start draining both queues as background tasks (call once, from the app's lifespan startup)."""
        self._tasks = [
            asyncio.create_task(self.drain_library_refresh()),
            asyncio.create_task(self.drain_enrichment()),
        ]

    async def stop(self) -> None:
        """Cancel both drain loops and wait for them to finish (call from the app's lifespan shutdown)."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks = []

    async def drain_library_refresh(self) -> None:
        """Process every message on the library-refresh queue until the receiver stops iterating.

        Each message's handling is wrapped in a broad ``except`` -- a failure anywhere inside
        :meth:`_handle` (including ``mark_running``/``mark_succeeded``/``mark_failed`` or the
        ``dead_letter_message``/``complete_message`` calls themselves, not just the job's own ``process``
        call) must never escape this loop. An uncaught exception here kills the ``asyncio.Task`` this method
        runs as (started once, in :meth:`start`) with no supervisor to restart it, permanently silencing
        this queue for every future message until the whole app restarts -- exactly the failure mode a
        first live production run surfaced (a single RAWG 401 during processing was logged and the run
        correctly marked ``failed``, but the subsequent ``dead_letter_message`` call apparently raised,
        and every later library-refresh message was left stuck at ``queued`` forever).
        """
        async for message in self._library_refresh_receiver:
            try:
                await self._handle(
                    message,
                    self._library_refresh_receiver,
                    required_fields=("run_id", "identity_sub"),
                    process=lambda payload: self._on_library_refresh(payload["identity_sub"]),
                )
            except Exception:
                logger.exception("Unhandled error draining a library-refresh message; continuing to drain")

    async def drain_enrichment(self) -> None:
        """Process every message on the enrichment queue until the receiver stops iterating.

        See :meth:`drain_library_refresh` for why every exception here is caught rather than allowed to
        kill this loop's background task.
        """
        async for message in self._enrichment_receiver:
            try:
                await self._handle(
                    message,
                    self._enrichment_receiver,
                    required_fields=("run_id",),
                    process=lambda _payload: self._on_enrichment_run(),
                )
            except Exception:
                logger.exception("Unhandled error draining an enrichment message; continuing to drain")

    async def _handle(
        self,
        message: Any,
        receiver: MessageReceiver,
        *,
        required_fields: tuple[str, ...],
        process: Any,
    ) -> None:
        self._lock_renewer.register(receiver, message)

        try:
            payload = json.loads(str(message))
        except (TypeError, ValueError):
            await receiver.dead_letter_message(
                message, reason="malformed-payload", error_description="invalid JSON body"
            )
            return

        if not isinstance(payload, dict):
            await receiver.dead_letter_message(
                message, reason="malformed-payload", error_description="body is not a JSON object"
            )
            return

        missing = [field for field in required_fields if field not in payload]
        if missing:
            await receiver.dead_letter_message(
                message, reason="malformed-payload", error_description=f"missing fields: {', '.join(missing)}"
            )
            return

        run_id = payload["run_id"]
        await self._job_runs_repository.mark_running(run_id)

        try:
            result_summary = await process(payload)
        except Exception as exc:
            # logger.exception still ships a full stack trace to Elasticsearch for operator debugging, but
            # what actually reaches job_runs.error / the browser / the dead-letter reason is always the
            # friendly, sanitized text below -- never the raw exception (see curator.jobs.error_messages).
            logger.exception("Job processing failed, dead-lettering message")
            friendly_error = friendly_job_error(exc)
            await self._job_runs_repository.mark_failed(run_id, friendly_error)
            await receiver.dead_letter_message(message, reason="processing-failed", error_description=friendly_error)
            return

        await self._job_runs_repository.mark_succeeded(run_id, result_summary)
        await receiver.complete_message(message)
