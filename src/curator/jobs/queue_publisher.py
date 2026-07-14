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
from typing import Any, Protocol

from azure.servicebus import ServiceBusMessage

from curator.jobs.repository import JobRunsRepository


class MessageSender(Protocol):
    """Duck-typed async Service Bus sender, satisfied by ``azure.servicebus.aio.ServiceBusSender``."""

    async def send_messages(self, message: Any) -> None:
        """Send one message (or a batch) to the sender's queue."""
        ...


class QueuePublisher:
    """Publishes library-refresh and enrichment-run job messages.

    :param library_refresh_sender: A sender bound to the ``curator-library-refresh`` queue.
    :param enrichment_sender: A sender bound to the ``curator-enrichment`` queue.
    :param job_runs_repository: Records each published run's ``queued`` status.
    """

    def __init__(
        self,
        *,
        library_refresh_sender: MessageSender,
        enrichment_sender: MessageSender,
        job_runs_repository: JobRunsRepository,
    ) -> None:
        self._library_refresh_sender = library_refresh_sender
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

    async def publish_enrichment_run(self) -> str:
        """Publish a global enrichment-catalog re-scrape job.

        :returns: The new run id.
        """
        run_id = str(uuid.uuid4())
        await self._job_runs_repository.create(run_id, "enrichment")
        body = json.dumps({"run_id": run_id})
        await self._enrichment_sender.send_messages(ServiceBusMessage(body))
        return run_id
