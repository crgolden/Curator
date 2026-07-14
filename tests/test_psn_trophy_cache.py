"""Tests for CachedTrophyClient, using a hand-written fake Redis and fake underlying TrophyClient."""

from __future__ import annotations

from curator.psn.models import TrophyCounts, TrophySummary, TrophyTitle
from curator.psn.trophy_cache import CachedTrophyClient


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.set_calls: list[tuple[str, str, int | None]] = []

    async def get(self, name):
        return self.store.get(name)

    async def set(self, name, value, ex=None):
        self.store[name] = value
        self.set_calls.append((name, value, ex))


class FakeTrophyClient:
    def __init__(self):
        self.summary_calls = 0
        self.titles_calls = 0

    async def trophy_summary(self, online_id=None, account_id=None):
        self.summary_calls += 1
        return TrophySummary(level=10, progress=50, tier=2, earned=TrophyCounts(gold=1), account_id="123")

    async def trophy_titles(self, online_id=None, account_id=None, limit=100):
        self.titles_calls += 1
        return [
            TrophyTitle(
                name="Game A",
                np_communication_id="NPWR1",
                platforms=("PS5",),
                progress=50,
                earned=TrophyCounts(gold=1),
                defined=TrophyCounts(gold=2),
            )
        ]


async def test_trophy_summary_calls_through_and_caches():
    client = FakeTrophyClient()
    redis = FakeRedis()
    cached = CachedTrophyClient(client, redis, ttl_seconds=900)

    first = await cached.trophy_summary()
    second = await cached.trophy_summary()

    assert first == second
    assert client.summary_calls == 1  # second call served from cache
    assert redis.set_calls[0][2] == 900


async def test_trophy_summary_different_targets_use_different_cache_keys():
    client = FakeTrophyClient()
    cached = CachedTrophyClient(client, FakeRedis())

    await cached.trophy_summary(online_id="Alice")
    await cached.trophy_summary(online_id="Bob")

    assert client.summary_calls == 2


async def test_trophy_titles_calls_through_and_caches():
    client = FakeTrophyClient()
    redis = FakeRedis()
    cached = CachedTrophyClient(client, redis)

    first = await cached.trophy_titles()
    second = await cached.trophy_titles()

    assert first == second
    assert client.titles_calls == 1


async def test_trophy_titles_different_limits_use_different_cache_keys():
    client = FakeTrophyClient()
    cached = CachedTrophyClient(client, FakeRedis())

    await cached.trophy_titles(limit=10)
    await cached.trophy_titles(limit=50)

    assert client.titles_calls == 2
