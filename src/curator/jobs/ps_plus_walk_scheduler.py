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

DEFAULT_MAX_PAGES_PER_CATEGORY = 20


class PsPlusWalker(Protocol):
    async def walk_all(self, *, max_pages_per_category: int | None = None) -> list[PsPlusWalkProgress]: ...


class WalkHistory(Protocol):
    async def latest_walk_started_at(self) -> datetime | None: ...


class PsPlusWalkScheduler:
    def __init__(
        self,
        walker: PsPlusWalker,
        history: WalkHistory,
        *,
        walk_interval: timedelta = DEFAULT_WALK_INTERVAL,
        check_interval_seconds: float = DEFAULT_CHECK_INTERVAL_SECONDS,
        max_pages_per_category: int = DEFAULT_MAX_PAGES_PER_CATEGORY,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._walker = walker
        self._history = history
        self._walk_interval = walk_interval
        self._check_interval_seconds = check_interval_seconds
        self._max_pages_per_category = max_pages_per_category
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
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
        try:
            if not await self.due():
                return False
            await self._walker.walk_all(max_pages_per_category=self._max_pages_per_category)
        except Exception:
            logger.exception("Scheduled PS Plus catalog walk failed")
            return False
        return True

    async def due(self) -> bool:
        latest = await self._history.latest_walk_started_at()
        return latest is None or self._clock() - latest >= self._walk_interval
