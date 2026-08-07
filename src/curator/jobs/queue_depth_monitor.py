"""Periodically polls Service Bus queue runtime properties into telemetry gauges.

Mirrors Functions' ``QueueDepthMonitorJob``/``Telemetry.cs`` for the .NET pipeline queues: nothing else
watches dead-lettered messages today (a message that fails processing dead-letters silently -- see
:mod:`curator.jobs.queue_consumer` -- with no alert until someone looks), so this polls each queue's
runtime properties into a gauge the fleet's "Service Bus dead-letter depth" alert style can read.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Sequence
from typing import Any, Protocol

from curator.telemetry import record_queue_depth

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL_SECONDS = 900.0


class QueueRuntimeProperties(Protocol):
    """The subset of ``azure.servicebus.management.QueueRuntimeProperties`` this monitor reads.

    Both counts are typed ``int | None`` because the real SDK properties are (they read through a nested,
    optional ``MessageCountDetails``) -- in practice always populated for a queue that actually exists.
    """

    @property
    def active_message_count(self) -> int | None: ...

    @property
    def dead_letter_message_count(self) -> int | None: ...


class QueueRuntimePropertiesReader(Protocol):
    """Duck-typed subset of ``azure.servicebus.management.ServiceBusAdministrationClient`` this monitor
    needs -- a plain sync client (the Python SDK has no async administration client), so every call is
    run via :func:`asyncio.to_thread` to avoid blocking the event loop.
    """

    def get_queue_runtime_properties(self, queue_name: str, **kwargs: Any) -> QueueRuntimeProperties:
        """Fetch a queue's current runtime properties (active/dead-letter counts, ...)."""
        ...


class QueueDepthMonitor:
    """Polls ``queue_names`` on a fixed interval, recording each queue's active/dead-letter counts into
    telemetry gauges (see :func:`curator.telemetry.record_queue_depth`).

    A polling failure for one queue (missing Manage claims, a transient admin-API error, ...) is logged and
    the loop continues to the next queue and the next cycle -- never allowed to kill the background task,
    mirroring ``QueueDepthMonitorJob``'s per-queue ``try``/``catch``.

    :param admin_client: The Service Bus administration client to poll.
    :param queue_names: The queue names to poll each cycle.
    :param poll_interval_seconds: Seconds between poll cycles; defaults to 15 minutes.
    """

    def __init__(
        self,
        admin_client: QueueRuntimePropertiesReader,
        queue_names: Sequence[str],
        *,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._admin_client = admin_client
        self._queue_names = queue_names
        self._poll_interval_seconds = poll_interval_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start polling as a background task (call once, from the app's lifespan startup)."""
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the polling loop and wait for it to finish (call from the app's lifespan shutdown)."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await self.poll_once()
            await asyncio.sleep(self._poll_interval_seconds)

    async def poll_once(self) -> None:
        """Poll every configured queue once, recording whatever succeeds and logging the rest."""
        for queue_name in self._queue_names:
            try:
                properties = await asyncio.to_thread(self._admin_client.get_queue_runtime_properties, queue_name)
            except Exception:
                logger.exception("Failed to poll Service Bus runtime properties for queue %s", queue_name)
                continue
            active_count, dead_letter_count = properties.active_message_count, properties.dead_letter_message_count
            if active_count is None or dead_letter_count is None:
                logger.warning("Service Bus queue %s reported no message counts; skipping this cycle", queue_name)
                continue
            record_queue_depth(queue_name, active_count, dead_letter_count)
