"""Publishes to ``curator-library-refresh``/``curator-enrichment`` via the ``azure-servicebus`` async
client.

Each publish generates the run id client-side, records a ``queued`` :class:`~curator.jobs.repository.JobRun`
row via the injected :class:`~curator.jobs.repository.JobRunsRepository`, then sends the message and
returns the run id immediately -- the caller (a route) never waits on the actual work, only on the row
insert and the message send. The row is what lets ``GET /library/refresh/{run_id}`` answer "is this done
yet" later; :mod:`curator.jobs.queue_consumer` advances its status as the job actually runs.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from azure.servicebus import ServiceBusMessage

from curator.jobs.repository import JobRunsRepository


class MessageSender(Protocol):
    """Duck-typed async Service Bus sender, satisfied by ``azure.servicebus.aio.ServiceBusSender``."""

    async def send_messages(self, message: Any) -> None:
        """Send one message (or a batch) to the sender's queue."""
        ...

    async def schedule_messages(self, messages: Any, schedule_time_utc: datetime) -> list[int]:
        """Send one message (or a batch), deferring visibility until ``schedule_time_utc``."""
        ...


class QueuePublisher:
    """Publishes library-refresh and enrichment-run job messages.

    :param library_refresh_sender: A sender bound to the ``curator-library-refresh`` queue.
    :param library_refresh_continuation_sender: A sender bound to the
        ``curator-library-refresh-continuation`` queue.
    :param enrichment_sender: A sender bound to the ``curator-enrichment`` queue.
    :param job_runs_repository: Records each published run's ``queued`` status.
    """

    def __init__(
        self,
        *,
        library_refresh_sender: MessageSender,
        library_refresh_continuation_sender: MessageSender,
        enrichment_sender: MessageSender,
        job_runs_repository: JobRunsRepository,
    ) -> None:
        self._library_refresh_sender = library_refresh_sender
        self._library_refresh_continuation_sender = library_refresh_continuation_sender
        self._enrichment_sender = enrichment_sender
        self._job_runs_repository = job_runs_repository

    async def publish_library_refresh(self, identity_sub: str) -> str:
        """Publish a library-refresh job for one user.

        :param identity_sub: The Curator user id (Identity's ``sub``) to refresh.
        :returns: The new run id.
        """
        run_id = str(uuid.uuid4())
        await self._job_runs_repository.create(run_id, "library_refresh", identity_sub)
        body = json.dumps({"run_id": run_id, "identity_sub": identity_sub})
        await self._library_refresh_sender.send_messages(ServiceBusMessage(body))
        return run_id

    async def publish_library_refresh_continuation(
        self,
        run_id: str,
        identity_sub: str,
        remaining_game_ids: list[str],
        provider: str,
        retry_after_seconds: float,
        *,
        seq: int,
    ) -> None:
        """Reschedule the remaining games of an in-progress library refresh that just hit a provider rate
        limit, to resume automatically once it lifts.

        Reuses ``run_id`` (never mints a new one) so ``GET /library/refresh/{run_id}`` keeps working across
        the whole retry chain. Sent via Service Bus's native scheduled-message feature -- the message stays
        invisible to :class:`~curator.jobs.queue_consumer.QueueConsumer`'s continuation drain loop until
        ``retry_after_seconds`` from now, so no separate polling/cron infrastructure is needed.

        :param run_id: The same run id the original ``curator-library-refresh`` message carried.
        :param identity_sub: The Curator user id (Identity's ``sub``) being refreshed.
        :param remaining_game_ids: Every game id not yet enriched (see
            ``curator.library.library_build_orchestrator.EnrichDeltaResult.remaining_game_ids``).
        :param provider: Which provider rate-limited (``"rawg"``/``"opencritic"``) -- carried in the body
            for observability even though ``retry_after_seconds`` already drives the scheduled time.
        :param retry_after_seconds: Seconds from now the limit should have lifted.
        :param seq: The run's new checkpoint sequence number (from
            ``JobRunsRepository.mark_rate_limited``'s return value). The receiving
            ``QueueConsumer._handle()`` uses this, via ``JobRunsRepository.try_begin_delivery``, to detect
            a stale redelivery of an earlier, already-superseded checkpoint.
        """
        body = json.dumps(
            {
                "run_id": run_id,
                "identity_sub": identity_sub,
                "remaining_game_ids": remaining_game_ids,
                "provider": provider,
                "retry_after_seconds": retry_after_seconds,
                "seq": seq,
            }
        )
        schedule_time_utc = datetime.now(timezone.utc) + timedelta(seconds=retry_after_seconds)
        await self._library_refresh_continuation_sender.schedule_messages(ServiceBusMessage(body), schedule_time_utc)

    async def publish_enrichment_run(self) -> str:
        """Publish a global enrichment-catalog re-scrape job.

        :returns: The new run id.
        """
        run_id = str(uuid.uuid4())
        await self._job_runs_repository.create(run_id, "enrichment")
        body = json.dumps({"run_id": run_id})
        await self._enrichment_sender.send_messages(ServiceBusMessage(body))
        return run_id
