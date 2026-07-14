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

from curator.jobs.repository import JobRunsRepository

logger = logging.getLogger(__name__)

LibraryRefreshHandler = Callable[[str], Coroutine[Any, Any, None]]
"""Processes one library-refresh job, given the target user's ``identity_sub``."""

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


class QueueConsumer:
    """Drains both job queues, dispatching each message to its handler.

    :param library_refresh_receiver: A receiver bound to the ``curator-library-refresh`` queue.
    :param enrichment_receiver: A receiver bound to the ``curator-enrichment`` queue.
    :param on_library_refresh: Called with ``identity_sub`` for each library-refresh message.
    :param on_enrichment_run: Called for each enrichment-run message.
    :param job_runs_repository: Advances each message's ``run_id`` through ``running``/``succeeded``/
        ``failed`` around dispatch.
    """

    def __init__(
        self,
        *,
        library_refresh_receiver: MessageReceiver,
        enrichment_receiver: MessageReceiver,
        on_library_refresh: LibraryRefreshHandler,
        on_enrichment_run: EnrichmentRunHandler,
        job_runs_repository: JobRunsRepository,
    ) -> None:
        self._library_refresh_receiver = library_refresh_receiver
        self._enrichment_receiver = enrichment_receiver
        self._on_library_refresh = on_library_refresh
        self._on_enrichment_run = on_enrichment_run
        self._job_runs_repository = job_runs_repository
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
        """Process every message on the library-refresh queue until the receiver stops iterating."""
        async for message in self._library_refresh_receiver:
            await self._handle(
                message,
                self._library_refresh_receiver,
                required_fields=("run_id", "identity_sub"),
                process=lambda payload: self._on_library_refresh(payload["identity_sub"]),
            )

    async def drain_enrichment(self) -> None:
        """Process every message on the enrichment queue until the receiver stops iterating."""
        async for message in self._enrichment_receiver:
            await self._handle(
                message,
                self._enrichment_receiver,
                required_fields=("run_id",),
                process=lambda _payload: self._on_enrichment_run(),
            )

    async def _handle(
        self,
        message: Any,
        receiver: MessageReceiver,
        *,
        required_fields: tuple[str, ...],
        process: Any,
    ) -> None:
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
            await process(payload)
        except Exception as exc:
            logger.exception("Job processing failed, dead-lettering message")
            await self._job_runs_repository.mark_failed(run_id, str(exc))
            await receiver.dead_letter_message(message, reason="processing-failed", error_description=str(exc))
            return

        await self._job_runs_repository.mark_succeeded(run_id)
        await receiver.complete_message(message)
