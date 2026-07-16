"""Builds the shared Redis client backing trophy caching (curator.psn.trophy_cache) and the distributed
PSN rate limiter (curator.psn.rate_limiter).

A single ``redis.asyncio.Redis`` connection pool is shared by both -- they write to disjoint key
namespaces (``curator:psn:trophy:*`` vs. ``curator:psn:ratelimit``), so there is no reason to open two
pools. Constructing the client never connects (redis-py is lazy: the first command opens the connection),
so this is safe to call even when Redis is unreachable -- a bad host only ever surfaces as an error on the
first actual cache/rate-limit call, never at startup.
"""

from __future__ import annotations

from typing import Any, cast

from redis.asyncio import Redis

from curator.settings import Settings


def build_redis_client(settings: Settings) -> Redis | None:
    """Build the shared Redis client from ``settings``, or ``None`` if Redis is not configured.

    :param settings: The resolved application settings.
    :returns: A ``redis.asyncio.Redis`` client, or ``None`` when ``settings.redis_host`` is unset.
    """
    if not settings.redis_host:
        return None
    return Redis(
        host=settings.redis_host,
        port=settings.redis_port,
        password=settings.redis_password,
        ssl=settings.redis_ssl,
    )


class RedisAdapter:
    """Narrows ``redis.asyncio.Redis``'s broad, heavily overloaded method signatures down to the exact
    async surface :class:`curator.psn.rate_limiter.RedisLike` and :class:`curator.psn.trophy_cache.RedisLike`
    each declare.

    Both are intentionally narrow, hand-written ``Protocol``s (structural, not the real client's type) so
    each module's tests can satisfy them with a minimal in-memory fake -- see ``FakeRedis`` in
    ``tests/test_psn_rate_limiter.py``/``tests/test_psn_trophy_cache.py``. The real client's stubs accept a
    wider parameter union (e.g. ``bytes | str | memoryview`` keys) and return ``Awaitable[Any]``, which
    mypy's strict structural check does not consider a match for the narrower protocols. Widening either
    protocol to the real client's shape would force every hand-written fake to widen in lockstep for no
    behavioral gain, since Curator only ever calls these methods with plain ``str``/``float``/``int``
    arguments. This adapter is the single seam that bridges the two: it re-declares each method at the
    narrow type the protocols expect and simply awaits the real client underneath.
    """

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def get(self, name: str) -> bytes | str | None:
        """See :meth:`curator.psn.trophy_cache.RedisLike.get`."""
        # redis-py's stubs return the untyped `Any` its heavy overloading collapses to; the actual runtime
        # value for a string GET is always bytes, str, or None (redis-py decodes per `decode_responses`).
        return cast("bytes | str | None", await self._redis.get(name))

    async def set(self, name: str, value: str, ex: int | None = None) -> Any:
        """See :meth:`curator.psn.trophy_cache.RedisLike.set`."""
        return await self._redis.set(name, value, ex=ex)

    async def delete(self, name: str) -> int:
        """See :meth:`curator.persistence.db_token_store.RedisLike.delete`."""
        return cast(int, await self._redis.delete(name))

    async def zremrangebyscore(self, name: str, min: float, max: float) -> int:
        """See :meth:`curator.psn.rate_limiter.RedisLike.zremrangebyscore`."""
        return cast(int, await self._redis.zremrangebyscore(name, min, max))

    async def zcard(self, name: str) -> int:
        """See :meth:`curator.psn.rate_limiter.RedisLike.zcard`."""
        return cast(int, await self._redis.zcard(name))

    async def zrange(self, name: str, start: int, end: int, *, withscores: bool = False) -> list[Any]:
        """See :meth:`curator.psn.rate_limiter.RedisLike.zrange`."""
        return cast("list[Any]", await self._redis.zrange(name, start, end, withscores=withscores))

    async def zadd(self, name: str, mapping: dict[str, float]) -> int:
        """See :meth:`curator.psn.rate_limiter.RedisLike.zadd`."""
        return cast(int, await self._redis.zadd(name, mapping))

    async def expire(self, name: str, seconds: int) -> bool:
        """See :meth:`curator.psn.rate_limiter.RedisLike.expire`."""
        return cast(bool, await self._redis.expire(name, seconds))
