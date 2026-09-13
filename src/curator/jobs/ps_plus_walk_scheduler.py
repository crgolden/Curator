"""Re-walks the PS Plus catalog on a weekly cadence from inside the API process.

The walk is bounded (a handful of pages per category) so it runs here rather than as a queued job; the
per-category advisory lock is what keeps two API workers from walking one category at once. The scheduler
is constructed by ``create_app`` and published on ``app.state``; whether the lifespan starts it is the
deployment decision recorded in ``AGENTS/REPOS/Curator.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Protocol

from curator.catalog.ps_plus_walk_service import PsPlusWalkProgress

logger = logging.getLogger(__name__)

DEFAULT_WALK_INTERVAL = timedelta(days=7)

DEFAULT_CHECK_INTERVAL_SECONDS = 3600.0


class PsPlusWalker(Protocol):
    """The slice of :class:`~curator.catalog.ps_plus_walk_service.PsPlusWalkService` the scheduler needs."""

    async def walk_all(self, *, max_pages_per_category: int | None = None) -> list[PsPlusWalkProgress]: ...


class WalkHistory(Protocol):
    """The slice of :class:`~curator.catalog.ps_plus_repository.PsPlusRepository` the scheduler needs."""

    async def latest_walk_started_at(self) -> datetime | None: ...


class PsPlusWalkScheduler:
    """Walks the catalog whenever the most recent walk is older than ``walk_interval``.

    The check runs every ``check_interval_seconds`` rather than sleeping for the whole interval, so a worker
    recycled mid-week neither re-walks on every start nor forgets the walk it was going to do.

    :param walker: What performs the walk.
    :param history: Where the last walk's start time is read.
    :param walk_interval: How old the last walk must be before another runs.
    :param check_interval_seconds: How often the age is checked.
    :param clock: Supplies the current instant.
    """

    def __init__(
        self,
        walker: PsPlusWalker,
        history: WalkHistory,
        *,
        walk_interval: timedelta = DEFAULT_WALK_INTERVAL,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._walker = walker
        self._history = history
        self._walk_interval = walk_interval
        self._check_interval_seconds = check_interval_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Start checking as a background task (call once, from the app's lifespan startup)."""
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel the background task and wait for it (call from the app's lifespan shutdown)."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await self.check_once()
            await asyncio.sleep(self._check_interval_seconds)

    async def check_once(self) -> bool:
        """Walk if the last walk is older than the interval, or if none has ever run.

        :returns: Whether a walk ran.
        """
        try:
            if not await self.due():
                return False
            await self._walker.walk_all()
        except Exception:
            logger.exception("Scheduled PS Plus catalog walk failed")
            return False
        return True

    async def due(self) -> bool:
        """Whether the most recent walk started more than ``walk_interval`` ago, or never."""
        latest = await self._history.latest_walk_started_at()
        return latest is None or self._clock() - latest >= self._walk_interval
