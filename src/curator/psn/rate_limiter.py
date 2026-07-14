"""Redis-backed distributed PSN rate limiter.

``psnpy``'s original rate limiter was an in-process ``collections.deque`` sliding window -- correct only
for a single-shot CLI process. Curator can scale out across multiple App Service instances, so each
instance tracking an independent budget could collectively exceed PSN's real 300-req/15-min limit. This
implements the same sliding-window algorithm against a Redis sorted set shared by every instance: each
request's timestamp is a member score, stale entries older than the window are trimmed on every call, and
the caller waits out however much of the window the oldest surviving entry still occupies once the budget
is full.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Protocol

from curator.psn.session import RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS


class RedisLike(Protocol):
    """The narrow slice of ``redis.asyncio.Redis``'s sorted-set API this limiter needs."""

    async def zremrangebyscore(self, name: str, min: float, max: float) -> int:
        """Remove members with a score in ``[min, max]``; returns the number removed."""
        ...

    async def zcard(self, name: str) -> int:
        """Return the number of members in the sorted set."""
        ...

    async def zrange(self, name: str, start: int, end: int, *, withscores: bool = False) -> list[Any]:
        """Return members in the given index range, optionally paired with their scores."""
        ...

    async def zadd(self, name: str, mapping: dict[str, float]) -> int:
        """Add members with the given scores."""
        ...

    async def expire(self, name: str, seconds: int) -> bool:
        """Set a TTL on the key so an idle limiter doesn't leak memory forever."""
        ...


class RedisRateLimiter:
    """A distributed sliding-window rate limiter over a shared Redis sorted set.

    :param redis: An async Redis client (``redis.asyncio.Redis`` or a hand-written fake satisfying
        :class:`RedisLike`).
    :param key: The sorted-set key; defaults to a namespaced constant shared by every request this budget
        governs.
    :param max_requests: The request budget per window; defaults to PSN's conservative 300.
    :param window_seconds: The sliding-window size in seconds; defaults to PSN's conservative 15 minutes.
    """

    def __init__(
        self,
        redis: RedisLike,
        *,
        key: str = "curator:psn:ratelimit",
        max_requests: int = RATE_LIMIT_MAX_REQUESTS,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self._redis = redis
        self._key = key
        self._max_requests = max_requests
        self._window_seconds = window_seconds

    async def acquire(self) -> None:
        """Block until this request is within the shared budget, then record it."""
        now = time.time()
        window_start = now - self._window_seconds
        await self._redis.zremrangebyscore(self._key, 0, window_start)
        count = await self._redis.zcard(self._key)
        if count >= self._max_requests:
            oldest = await self._redis.zrange(self._key, 0, 0, withscores=True)
            if oldest:
                _, oldest_score = oldest[0]
                wait = self._window_seconds - (now - oldest_score)
                if wait > 0:
                    await asyncio.sleep(wait)
        await self._redis.zadd(self._key, {str(uuid.uuid4()): time.time()})
        await self._redis.expire(self._key, int(self._window_seconds) + 60)
