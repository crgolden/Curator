"""Publishes to ``curator-library-refresh``/``curator-enrichment`` via the ``azure-servicebus`` async
client.

Each publish generates the run id client-side, records a ``queued`` :class:`~curator.jobs.repository.JobRun`
row via the injected :class:`~curator.jobs.repository.JobRunsRepository`, then sends the message and
returns the run id immediately -- the caller (a route) never waits on the actual work, only on the row
insert and the message send. The row is what lets ``GET /library/refresh/{run_id}`` answer "is this done
yet" later; something outside this codebase advances its status as the job actually runs.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
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
    :param enrichment_sender: A sender bound to the ``curator-enrichment`` queue.
    :param scheduled_refresh_sender: A sender bound to the ``curator-scheduled-refresh`` queue, or
        ``None`` where that queue is not provisioned.
    :param job_runs_repository: Records each published run's ``queued`` status.
    """

    def __init__(
        self,
        *,
        library_refresh_sender: MessageSender,
        enrichment_sender: MessageSender,
        job_runs_repository: JobRunsRepository,
        scheduled_refresh_sender: MessageSender | None = None,
    ) -> None:
        self._library_refresh_sender = library_refresh_sender
        self._enrichment_sender = enrichment_sender
        self._scheduled_refresh_sender = scheduled_refresh_sender
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

    async def publish_scheduled_library_refresh(self, identity_sub: str, scheduled_for: datetime) -> None:
        """Defer a library refresh until ``scheduled_for``, for a user who opted in to a recurring schedule.

        Creates no ``job_runs`` row. The other publish methods create one immediately because their work
        starts immediately, but a scheduled message can sit invisible for a month, and a ``queued`` row is
        non-terminal to
        :meth:`~curator.jobs.repository.JobRunsRepository.find_active_run` -- so a row created here would
        make ``POST /library/refresh``'s duplicate-run guard hand the user this pending run instead of
        starting a real one, for as long as the wait lasts. The processing runtime creates the row when it
        actually begins.

        ``scheduled_for`` is echoed in the body so the processor can compare it against the row's current
        ``next_run_at`` and discard a superseded message: re-saving a schedule publishes a new message
        without cancelling the old one, the same staleness-checkpoint approach ``seq`` already gives the
        rate-limit continuation chain.

        :param identity_sub: The Curator user id (Identity's ``sub``) to refresh.
        :param scheduled_for: When the refresh should become visible to the processor.
        :raises RuntimeError: If no scheduled-refresh sender is configured.
        """
        if self._scheduled_refresh_sender is None:
            raise RuntimeError("No scheduled-refresh queue is configured.")
        body = json.dumps({"identity_sub": identity_sub, "scheduled_for": scheduled_for.isoformat()})
        await self._scheduled_refresh_sender.schedule_messages(ServiceBusMessage(body), scheduled_for)

    async def publish_enrichment_run(self) -> str:
        """Publish a global enrichment-catalog re-scrape job.

        :returns: The new run id.
        """
        run_id = str(uuid.uuid4())
        await self._job_runs_repository.create(run_id, "enrichment")
        body = json.dumps({"run_id": run_id})
        await self._enrichment_sender.send_messages(ServiceBusMessage(body))
        return run_id
