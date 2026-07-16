"""Tests for CachedTrophyClient, using a hand-written fake Redis and fake underlying TrophyClient."""

from __future__ import annotations

from curator.psn.models import TitleStat, TrophyCounts, TrophyDetail, TrophyGroups, TrophySummary, TrophyTitle
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
        self.titles_for_title_calls = []
        self.title_trophies_calls = []
        self.trophy_groups_calls = []
        self.title_stats_calls = 0

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

    async def trophy_titles_for_title(self, title_ids, online_id=None, account_id=None):
        self.titles_for_title_calls.append((tuple(title_ids), online_id, account_id))
        return []

    async def title_trophies(
        self, np_communication_id, platform, online_id=None, account_id=None, group="all", limit=None
    ):
        self.title_trophies_calls.append((np_communication_id, platform, online_id, account_id, group, limit))
        return [TrophyDetail(trophy_id=1, name="First Blood", detail="Do the thing", earned=True, rarity=42.0)]

    async def trophy_groups(self, np_communication_id, platform, online_id=None, account_id=None):
        self.trophy_groups_calls.append((np_communication_id, platform, online_id, account_id))
        return TrophyGroups(
            title_name="Game A",
            platforms=("PS5",),
            progress=50,
            defined=TrophyCounts(gold=2),
            earned=TrophyCounts(gold=1),
            groups=(),
        )

    async def title_stats(self, online_id=None, account_id=None, limit=200):
        self.title_stats_calls += 1
        return [TitleStat(title_id="CUSA00419_00", name="Game A", play_count=3)]


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


async def test_trophy_titles_for_title_passes_through_uncached():
    client = FakeTrophyClient()
    redis = FakeRedis()
    cached = CachedTrophyClient(client, redis)

    result = await cached.trophy_titles_for_title(["CUSA00419_00"], online_id="Alice")

    assert result == []
    assert client.titles_for_title_calls == [(("CUSA00419_00",), "Alice", None)]
    assert redis.set_calls == []


async def test_title_trophies_passes_through_uncached():
    client = FakeTrophyClient()
    redis = FakeRedis()
    cached = CachedTrophyClient(client, redis)

    result = await cached.title_trophies("NPWR1", "PS5", group="default", limit=10)

    assert result[0].name == "First Blood"
    assert client.title_trophies_calls == [("NPWR1", "PS5", None, None, "default", 10)]
    assert redis.set_calls == []


async def test_trophy_groups_passes_through_uncached():
    client = FakeTrophyClient()
    redis = FakeRedis()
    cached = CachedTrophyClient(client, redis)

    result = await cached.trophy_groups("NPWR1", "PS5", account_id="123")

    assert result.title_name == "Game A"
    assert client.trophy_groups_calls == [("NPWR1", "PS5", None, "123")]
    assert redis.set_calls == []


async def test_title_stats_passes_through_uncached():
    client = FakeTrophyClient()
    redis = FakeRedis()
    cached = CachedTrophyClient(client, redis)

    result = await cached.title_stats(limit=5)

    assert result[0].title_id == "CUSA00419_00"
    assert client.title_stats_calls == 1
    assert redis.set_calls == []
