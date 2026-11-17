"""Tests for RedisRateLimiter's sliding-window throttling, using a hand-written fake Redis sorted set."""

from __future__ import annotations

import asyncio
import time

from curator.psn.rate_limiter import RedisRateLimiter
from test_values import lowercase_token, new_positive_count, new_short_interval, new_small_count


class FakeRedis:
    """Stands in for ``redis.asyncio.Redis``'s sorted-set API: an in-memory dict of member -> score."""

    def __init__(self) -> None:
        self.members: dict[str, float] = {}
        self.expire_calls: list[tuple[str, int]] = []

    async def zremrangebyscore(self, name: str, min: float, max: float) -> int:
        to_remove = [member for member, score in self.members.items() if min <= score <= max]
        for member in to_remove:
            del self.members[member]
        return len(to_remove)

    async def zcard(self, name: str) -> int:
        return len(self.members)

    async def zrange(self, name: str, start: int, end: int, *, withscores: bool = False) -> list:
        ordered = sorted(self.members.items(), key=lambda item: item[1])
        sliced = ordered[start : end + 1] if end >= 0 else ordered[start:]
        return sliced if withscores else [member for member, _ in sliced]

    async def zadd(self, name: str, mapping: dict[str, float]) -> int:
        self.members.update(mapping)
        return len(mapping)

    async def expire(self, name: str, seconds: int) -> bool:
        self.expire_calls.append((name, seconds))
        return True


async def test_acquire_under_budget_does_not_sleep(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", _record_sleep(sleeps))
    redis = FakeRedis()
    limiter = RedisRateLimiter(redis, max_requests=new_small_count(), window_seconds=new_positive_count())

    await limiter.acquire()

    assert sleeps == []
    assert len(redis.members) == 1


async def test_acquire_over_budget_sleeps_out_the_oldest_entry(monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(asyncio, "sleep", _record_sleep(sleeps))
    redis = FakeRedis()
    now = time.time()
    max_requests = new_small_count()
    seed_age_seconds = new_small_count()
    redis.members = {lowercase_token(): now - seed_age_seconds for _ in range(max_requests)}
    limiter = RedisRateLimiter(redis, max_requests=max_requests, window_seconds=new_short_interval().total_seconds())

    await limiter.acquire()

    assert len(sleeps) == 1
    assert sleeps[0] > 0
    assert len(redis.members) == max_requests + 1


async def test_acquire_prunes_entries_older_than_the_window():
    redis = FakeRedis()
    now = time.time()
    window_seconds = new_short_interval().total_seconds()
    stale_member = lowercase_token()
    redis.members = {stale_member: now - window_seconds - new_positive_count()}
    limiter = RedisRateLimiter(redis, max_requests=new_small_count(), window_seconds=window_seconds)

    await limiter.acquire()

    assert stale_member not in redis.members
    assert len(redis.members) == 1


async def test_acquire_sets_expiry_on_the_key():
    redis = FakeRedis()
    key = lowercase_token()
    limiter = RedisRateLimiter(redis, key=key, max_requests=new_small_count(), window_seconds=60)

    await limiter.acquire()

    assert redis.expire_calls[-1] == (key, 120)


def _record_sleep(sleeps: list[float]):
    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)

    return _sleep
