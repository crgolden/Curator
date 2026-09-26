"""Tests for CachedTrophyClient, using a hand-written fake Redis and fake underlying TrophyClient."""

from __future__ import annotations

from curator.psn.models import TitleStat, TrophyCounts, TrophyDetail, TrophyGroups, TrophySummary, TrophyTitle
from curator.psn.title_platform import PS5
from curator.psn.trophy_cache import DEFAULT_TTL_SECONDS, CachedTrophyClient
from test_values import (
    lowercase_token,
    new_account_id,
    new_game_title,
    new_np_communication_id,
    new_online_id,
    new_percent_completed,
    new_positive_count,
    new_ps4_title_id,
    new_small_count,
    new_trophy_group_id,
)


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
        self.summary = TrophySummary(
            level=new_positive_count(),
            progress=new_percent_completed(),
            tier=new_small_count(),
            earned=TrophyCounts(gold=new_small_count()),
            account_id=new_account_id(),
        )
        self.titles = [
            TrophyTitle(
                name=new_game_title(),
                np_communication_id=new_np_communication_id(),
                platforms=(PS5,),
                progress=new_percent_completed(),
                earned=TrophyCounts(gold=new_small_count()),
                defined=TrophyCounts(gold=new_small_count()),
            )
        ]
        self.details = [TrophyDetail(trophy_id=new_positive_count(), name=new_game_title(), detail=lowercase_token())]
        self.groups = TrophyGroups(
            title_name=new_game_title(),
            platforms=(PS5,),
            progress=new_percent_completed(),
            defined=TrophyCounts(gold=new_small_count()),
            earned=TrophyCounts(gold=new_small_count()),
            groups=(),
        )
        self.stats = [TitleStat(title_id=new_ps4_title_id(), name=new_game_title(), play_count=new_small_count())]
        self.summary_calls = 0
        self.titles_calls = 0
        self.titles_for_title_calls = []
        self.title_trophies_calls = []
        self.trophy_groups_calls = []
        self.title_stats_calls = 0

    async def trophy_summary(self, online_id=None, account_id=None):
        self.summary_calls += 1
        return self.summary

    async def trophy_titles(self, online_id=None, account_id=None, limit=100):
        self.titles_calls += 1
        return self.titles

    async def trophy_titles_for_title(self, title_ids, online_id=None, account_id=None):
        self.titles_for_title_calls.append((tuple(title_ids), online_id, account_id))
        return []

    async def title_trophies(
        self, np_communication_id, platform, online_id=None, account_id=None, group=None, limit=None
    ):
        self.title_trophies_calls.append((np_communication_id, platform, online_id, account_id, group, limit))
        return self.details

    async def trophy_groups(self, np_communication_id, platform, online_id=None, account_id=None):
        self.trophy_groups_calls.append((np_communication_id, platform, online_id, account_id))
        return self.groups

    async def title_stats(self, online_id=None, account_id=None, limit=200):
        self.title_stats_calls += 1
        return self.stats


async def test_trophy_summary_calls_through_and_caches():
    client = FakeTrophyClient()
    redis = FakeRedis()
    cached = CachedTrophyClient(client, redis)

    first = await cached.trophy_summary()
    second = await cached.trophy_summary()

    assert first == second == client.summary
    assert client.summary_calls == 1
    assert redis.set_calls[0][2] == DEFAULT_TTL_SECONDS


async def test_trophy_summary_honours_a_configured_ttl():
    ttl_seconds = new_positive_count()
    redis = FakeRedis()

    await CachedTrophyClient(FakeTrophyClient(), redis, ttl_seconds=ttl_seconds).trophy_summary()

    assert redis.set_calls[0][2] == ttl_seconds


async def test_trophy_summary_different_targets_use_different_cache_keys():
    client = FakeTrophyClient()
    cached = CachedTrophyClient(client, FakeRedis())

    await cached.trophy_summary(online_id=new_online_id())
    await cached.trophy_summary(online_id=new_online_id())

    assert client.summary_calls == 2


async def test_trophy_titles_calls_through_and_caches():
    client = FakeTrophyClient()
    cached = CachedTrophyClient(client, FakeRedis())

    first = await cached.trophy_titles()
    second = await cached.trophy_titles()

    assert first == second == client.titles
    assert client.titles_calls == 1


async def test_trophy_titles_different_limits_use_different_cache_keys():
    client = FakeTrophyClient()
    cached = CachedTrophyClient(client, FakeRedis())
    first_limit = new_positive_count()

    await cached.trophy_titles(limit=first_limit)
    await cached.trophy_titles(limit=first_limit + 1)

    assert client.titles_calls == 2


async def test_trophy_titles_for_title_passes_through_uncached():
    client = FakeTrophyClient()
    redis = FakeRedis()
    title_id, online_id = new_ps4_title_id(), new_online_id()

    result = await CachedTrophyClient(client, redis).trophy_titles_for_title([title_id], online_id=online_id)

    assert result == []
    assert client.titles_for_title_calls == [((title_id,), online_id, None)]
    assert redis.set_calls == []


async def test_title_trophies_passes_through_uncached():
    client = FakeTrophyClient()
    redis = FakeRedis()
    np_communication_id, group, limit = new_np_communication_id(), new_trophy_group_id(), new_positive_count()

    result = await CachedTrophyClient(client, redis).title_trophies(np_communication_id, PS5, group=group, limit=limit)

    assert result == client.details
    assert client.title_trophies_calls == [(np_communication_id, PS5, None, None, group, limit)]
    assert redis.set_calls == []


async def test_trophy_groups_passes_through_uncached():
    client = FakeTrophyClient()
    redis = FakeRedis()
    np_communication_id, account_id = new_np_communication_id(), new_account_id()

    result = await CachedTrophyClient(client, redis).trophy_groups(np_communication_id, PS5, account_id=account_id)

    assert result == client.groups
    assert client.trophy_groups_calls == [(np_communication_id, PS5, None, account_id)]
    assert redis.set_calls == []


async def test_title_stats_passes_through_uncached():
    client = FakeTrophyClient()
    redis = FakeRedis()

    result = await CachedTrophyClient(client, redis).title_stats(limit=new_positive_count())

    assert result == client.stats
    assert client.title_stats_calls == 1
    assert redis.set_calls == []
