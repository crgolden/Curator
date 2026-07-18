"""Tests for build_redis_client (Settings -> optional Redis client) and RedisAdapter (narrows the real
client down to the exact surface curator.psn.rate_limiter.RedisLike/curator.psn.trophy_cache.RedisLike
declare), using a hand-written fake standing in for redis.asyncio.Redis's broader method shape.
"""

from __future__ import annotations

from typing import Any

from redis.asyncio import Redis

from curator.redis_client import RedisAdapter, build_redis_client
from curator.settings import Settings


def _settings(**overrides) -> Settings:
    values: dict[str, Any] = dict(
        oidc_authority="https://identity.example.test", token_key="key", database_url="postgresql://unused"
    )
    values.update(overrides)
    return Settings(**values)


def test_build_redis_client_returns_none_when_host_unset():
    assert build_redis_client(_settings()) is None


def test_build_redis_client_builds_real_client_when_host_set():
    client = build_redis_client(_settings(redis_host="redis.example.test", redis_port=6380, redis_password="secret"))
    assert isinstance(client, Redis)


class FakeRawRedis:
    """Stands in for redis.asyncio.Redis's broader async surface (keyword names/param widths differ from
    curator.psn.*.RedisLike -- e.g. real ``expire`` takes ``time``, not ``seconds``) with call recording."""

    def __init__(self):
        self.strings: dict[str, str] = {}
        self.zsets: dict[str, dict[str, float]] = {}
        self.set_calls: list[tuple[str, str, int | None]] = []
        self.expire_calls: list[tuple[str, int]] = []

    async def get(self, name):
        return self.strings.get(name)

    async def set(self, name, value, ex=None):
        self.strings[name] = value
        self.set_calls.append((name, value, ex))
        return True

    async def zremrangebyscore(self, name, min, max):
        members = self.zsets.setdefault(name, {})
        to_remove = [member for member, score in members.items() if min <= score <= max]
        for member in to_remove:
            del members[member]
        return len(to_remove)

    async def zcard(self, name):
        return len(self.zsets.get(name, {}))

    async def zrange(self, name, start, end, withscores=False):
        ordered = sorted(self.zsets.get(name, {}).items(), key=lambda item: item[1])
        sliced = ordered[start : end + 1] if end >= 0 else ordered[start:]
        return sliced if withscores else [member for member, _ in sliced]

    async def zadd(self, name, mapping):
        self.zsets.setdefault(name, {}).update(mapping)
        return len(mapping)

    async def expire(self, name, seconds):
        self.expire_calls.append((name, seconds))
        return True


async def test_adapter_get_set_round_trip():
    raw = FakeRawRedis()
    adapter = RedisAdapter(raw)

    await adapter.set("k", "v", ex=60)
    assert await adapter.get("k") == "v"
    assert raw.set_calls == [("k", "v", 60)]


async def test_adapter_zset_methods_pass_through():
    raw = FakeRawRedis()
    adapter = RedisAdapter(raw)

    assert await adapter.zadd("z", {"a": 1.0, "b": 2.0}) == 2
    assert await adapter.zcard("z") == 2
    assert await adapter.zrange("z", 0, -1) == ["a", "b"]
    assert await adapter.zrange("z", 0, 0, withscores=True) == [("a", 1.0)]
    assert await adapter.zremrangebyscore("z", 0, 1) == 1
    assert await adapter.zcard("z") == 1
    assert await adapter.expire("z", 30) is True
    assert raw.expire_calls == [("z", 30)]
