"""Async consumer draining ``curator-library-refresh``/``curator-library-refresh-continuation``/
``curator-enrichment``, dispatching to
``curator.library.library_build_orchestrator``/``curator.enrichment.enrichment_service``.

Started in ``create_app()``'s lifespan, runs inside the same Curator process -- no second deployable
needed. Malformed messages (invalid JSON, missing required fields) and messages whose processing raises an
exception both dead-letter rather than being silently completed or retried forever, mirroring Functions'
``deadLetterReason: "malformed-payload"`` convention. Every message also carries a ``run_id`` (created in
``queued`` status by :class:`~curator.jobs.queue_publisher.QueuePublisher` before the message was even
sent) and, for a continuation message, a ``seq`` checkpoint number (``0`` if absent). Before dispatching,
this consumer calls :meth:`~curator.jobs.repository.JobRunsRepository.try_begin_delivery` -- a
compare-and-swap that only transitions the run to ``running`` (and lets processing proceed) if ``seq``
still matches the run's current checkpoint and the run has not already reached a terminal status; a stale
redelivery is settled without reprocessing instead. What "settled" means depends on why it was stale: a
run that already ``succeeded``, or whose checkpoint has since been superseded by a later one, is silently
completed -- nothing went wrong, this is just settlement noise. A run that already ``failed`` is
dead-lettered instead (using the run's already-recorded error), matching every other failure path in this
module, so a redelivery of a failed run's message still surfaces for operator follow-up rather than
silently vanishing from both the active queue and the dead-letter sub-queue. A successful dispatch is
followed by ``succeeded``/``failed`` via the same injected
:class:`~curator.jobs.repository.JobRunsRepository` -- this is what ``GET /library/refresh/{run_id}``
reads.

Settling a message (``complete_message``/``dead_letter_message``) always goes through :meth:`QueueConsumer
._settle`, which retries a real Service Bus connectivity blip a bounded number of times and separately
tolerates -- by logging and moving on, never retrying and never re-raising -- the message's lock having
already been lost to the broker (``MessageLockLostError``/``SessionLockLostError``). A lock loss can happen
when this consumer's own processing runs long enough that the lock lapses before a settle call is reached;
the resulting redelivery is exactly what ``try_begin_delivery``'s checkpoint guard exists to make safe, so
this consumer no longer needs to prevent it, only tolerate it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable, Coroutine
from typing import Any, Protocol

from azure.servicebus.exceptions import (
    MessageLockLostError,
    OperationTimeoutError,
    ServiceBusCommunicationError,
    ServiceBusConnectionError,
    ServiceBusServerBusyError,
    SessionLockLostError,
)
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from curator.jobs.error_messages import friendly_job_error
from curator.jobs.repository import JobRunsRepository

logger = logging.getLogger(__name__)

_TRANSIENT_SERVICE_BUS_ERRORS = (
    ServiceBusConnectionError,
    ServiceBusCommunicationError,
    ServiceBusServerBusyError,
    OperationTimeoutError,
)
"""Worth a bounded retry -- a real connectivity blip settling a message, not a lock that's already
gone. Deliberately excludes MessageLockLostError/SessionLockLostError: once the broker has reassigned
a message's lock, retrying the exact same settle call cannot succeed, so those are caught separately
and never retried (see QueueConsumer._settle)."""


@retry(
    retry=retry_if_exception_type(_TRANSIENT_SERVICE_BUS_ERRORS),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=0.5, max=5),
    reraise=True,
)
async def _settle_with_retry(
    receiver: MessageReceiver,
    message: Any,
    *,
    dead_letter: bool,
    reason: str | None = None,
    error_description: str | None = None,
) -> None:
    if dead_letter:
        assert reason is not None, "dead_letter=True requires a reason (every internal call site provides one)"
        await receiver.dead_letter_message(message, reason=reason, error_description=error_description)
    else:
        await receiver.complete_message(message)


LibraryRefreshHandler = Callable[[str, str], Coroutine[Any, Any, dict[str, Any] | None]]
"""Processes one library-refresh job, given its ``run_id`` and the target user's ``identity_sub`` --
``run_id`` is needed so a handler that hits a rate limit can perform its own ``mark_rate_limited`` +
republish and raise :class:`RateLimitRetryScheduled`, rather than only being able to return a result summary
for the default ``mark_succeeded`` path. Returns an optional result-summary payload for
``JobRunsRepository.mark_succeeded`` otherwise."""

LibraryRefreshContinuationHandler = Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any] | None]]
"""Processes one library-refresh continuation job, given the full message payload (``run_id``,
``identity_sub``, ``remaining_game_ids``, ``provider``, ``retry_after_seconds``); returns an optional
result-summary payload for ``JobRunsRepository.mark_succeeded`` -- or raises :class:`RateLimitRetryScheduled`
if it hit the rate limit again and has already republished (see that exception's docstring)."""

EnrichmentRunHandler = Callable[[], Coroutine[Any, Any, dict[str, Any] | None]]
"""Processes one global enrichment-catalog re-scrape job. Returns an optional result-summary payload for
``JobRunsRepository.mark_succeeded`` describing each pass's outcome (provider availability, auth/rate-limit
errors, rule-set-unchanged skips) -- never raises for a provider being unconfigured, auth-rejected, or
rate-limited (see ``curator.app._enrichment_run_handler``)."""


class RateLimitRetryScheduled(Exception):
    """Raised by a library-refresh-continuation handler that hit the rate limit again and has already
    performed its own ``mark_rate_limited`` + republish (see
    ``curator.app._library_refresh_continuation_handler``).

    :meth:`QueueConsumer._handle` special-cases this before its generic ``except Exception`` branch: it
    just completes the message and returns, with no additional status write or dead-letter -- both of the
    usual post-processing side effects were already performed by the handler itself before raising this.
    """


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
    :param library_refresh_continuation_receiver: A receiver bound to the
        ``curator-library-refresh-continuation`` queue.
    :param enrichment_receiver: A receiver bound to the ``curator-enrichment`` queue.
    :param on_library_refresh: Called with ``identity_sub`` for each library-refresh message.
    :param on_library_refresh_continuation: Called with the full payload for each library-refresh
        continuation message.
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
        library_refresh_continuation_receiver: MessageReceiver,
        enrichment_receiver: MessageReceiver,
        on_library_refresh: LibraryRefreshHandler,
        on_library_refresh_continuation: LibraryRefreshContinuationHandler,
        on_enrichment_run: EnrichmentRunHandler,
        job_runs_repository: JobRunsRepository,
        lock_renewer: LockRenewer | None = None,
    ) -> None:
        self._library_refresh_receiver = library_refresh_receiver
        self._library_refresh_continuation_receiver = library_refresh_continuation_receiver
        self._enrichment_receiver = enrichment_receiver
        self._on_library_refresh = on_library_refresh
        self._on_library_refresh_continuation = on_library_refresh_continuation
        self._on_enrichment_run = on_enrichment_run
        self._job_runs_repository = job_runs_repository
        self._lock_renewer = lock_renewer or NullLockRenewer()
        self._tasks: list[asyncio.Task[None]] = []

    def start(self) -> None:
        """Start draining every queue as background tasks (call once, from the app's lifespan startup)."""
        self._tasks = [
            asyncio.create_task(self.drain_library_refresh()),
            asyncio.create_task(self.drain_library_refresh_continuation()),
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
        :meth:`_handle` (including ``try_begin_delivery``/``mark_succeeded``/``mark_failed`` or the
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
                    process=lambda payload: self._on_library_refresh(payload["run_id"], payload["identity_sub"]),
                )
            except Exception:
                logger.exception("Unhandled error draining a library-refresh message; continuing to drain")

    async def drain_library_refresh_continuation(self) -> None:
        """Process every message on the library-refresh continuation queue until the receiver stops
        iterating.

        See :meth:`drain_library_refresh` for why every exception here is caught rather than allowed to
        kill this loop's background task. A message only ever becomes visible here once its scheduled
        ``retry_after_seconds`` delay has elapsed (see
        ``curator.jobs.queue_publisher.QueuePublisher.publish_library_refresh_continuation``).
        """
        async for message in self._library_refresh_continuation_receiver:
            try:
                await self._handle(
                    message,
                    self._library_refresh_continuation_receiver,
                    required_fields=("run_id", "identity_sub", "remaining_game_ids", "provider", "retry_after_seconds"),
                    process=self._on_library_refresh_continuation,
                )
            except Exception:
                logger.exception("Unhandled error draining a library-refresh-continuation message; continuing to drain")

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

    async def _settle(
        self,
        receiver: MessageReceiver,
        message: Any,
        *,
        dead_letter: bool,
        reason: str | None = None,
        error_description: str | None = None,
    ) -> None:
        """Complete or dead-letter ``message``, tolerating the two ways this can legitimately fail.

        A real connectivity blip is retried a few times (`_settle_with_retry`'s tenacity wrapper) since it
        might succeed on the next attempt. A lock-loss error is different in kind, not degree: once Service
        Bus has already reassigned the message's lock (because our own processing ran long enough that the
        lock lapsed before we got here), no amount of retrying this exact call can succeed -- the broker has
        already made the message available for redelivery elsewhere. This is caught and logged, not
        retried and not re-raised: whatever work this call was settling (mark_rate_limited/mark_succeeded/
        mark_failed) already committed to job_runs before this call ran, so no data is lost -- only this
        settlement attempt itself failed, and the resulting redelivery is exactly what
        ``JobRunsRepository.try_begin_delivery``'s checkpoint guard exists to make safe.
        """
        try:
            await _settle_with_retry(
                receiver, message, dead_letter=dead_letter, reason=reason, error_description=error_description
            )
        except (MessageLockLostError, SessionLockLostError):
            logger.warning(
                "Lock lost settling a message (dead_letter=%s, reason=%s) -- already reassigned by the "
                "broker for redelivery; the underlying job_runs update already committed",
                dead_letter,
                reason,
            )

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
            await self._settle(
                receiver, message, dead_letter=True, reason="malformed-payload", error_description="invalid JSON body"
            )
            return

        if not isinstance(payload, dict):
            await self._settle(
                receiver,
                message,
                dead_letter=True,
                reason="malformed-payload",
                error_description="body is not a JSON object",
            )
            return

        missing = [field for field in required_fields if field not in payload]
        if missing:
            await self._settle(
                receiver,
                message,
                dead_letter=True,
                reason="malformed-payload",
                error_description=f"missing fields: {', '.join(missing)}",
            )
            return

        run_id = payload["run_id"]
        expected_seq = payload.get("seq", 0)
        began = await self._job_runs_repository.try_begin_delivery(run_id, expected_seq)
        if not began:
            current = await self._job_runs_repository.get(run_id)
            if current is not None and current.status == "failed":
                # Unlike a superseded checkpoint or an already-succeeded run (nothing went wrong in
                # either case), this redelivery belongs to a run that already failed -- most likely
                # because the original dead_letter_message call for it failed too (see _settle). Silently
                # completing here would make the message vanish from both the active queue and the DLQ,
                # with zero operator visibility that anything ever failed -- inconsistent with every other
                # failure path in this module, which always dead-letters. Dead-letter it instead, using the
                # error already recorded on the run, so it still surfaces for follow-up.
                logger.warning(
                    "Stale redelivery for run %s of an already-failed run -- dead-lettering instead of "
                    "silently completing so it still surfaces in the DLQ for operator follow-up",
                    run_id,
                )
                await self._settle(
                    receiver,
                    message,
                    dead_letter=True,
                    reason="processing-failed",
                    error_description=current.error or "run already failed",
                )
                return
            logger.info(
                "Stale redelivery for run %s (seq %s no longer current, or run already finished) -- "
                "settling without reprocessing",
                run_id,
                expected_seq,
            )
            await self._settle(receiver, message, dead_letter=False)
            return

        try:
            result_summary = await process(payload)
        except RateLimitRetryScheduled:
            # The handler already ran mark_rate_limited + republished the remaining games itself -- no
            # further status write, and definitely no dead-letter (this isn't a failure).
            await self._settle(receiver, message, dead_letter=False)
            return
        except Exception as exc:
            # logger.exception still ships a full stack trace to Elasticsearch for operator debugging, but
            # what actually reaches job_runs.error / the browser / the dead-letter reason is always the
            # friendly, sanitized text below -- never the raw exception (see curator.jobs.error_messages).
            logger.exception("Job processing failed, dead-lettering message")
            friendly_error = friendly_job_error(exc)
            await self._job_runs_repository.mark_failed(run_id, friendly_error)
            await self._settle(
                receiver, message, dead_letter=True, reason="processing-failed", error_description=friendly_error
            )
            return

        await self._job_runs_repository.mark_succeeded(run_id, result_summary)
        await self._settle(receiver, message, dead_letter=False)
