"""Tests for TrophyClient, using a hand-written fake session (no network, no credentials).

Ported from ``psnpy``'s ``test_capabilities.py``, split to the trophy-data subset.
"""

from __future__ import annotations

import random

from curator.psn._identity import ACCOUNT_ID_KEY, MY_ACCOUNT_URL, SELF_PATH_ID
from curator.psn.models import (
    BRONZE_KEY,
    GOLD_KEY,
    TitleStat,
    TrophyCounts,
    TrophyDetail,
    TrophyGroup,
    TrophyGroups,
    TrophySummary,
)
from curator.psn.title_platform import PS4, PS5
from curator.psn.trophy_client import (
    CATEGORY_KEY,
    EARNED_DATE_KEY,
    EARNED_KEY,
    EARNED_TROPHIES_KEY,
    NAME_KEY,
    NEXT_OFFSET_KEY,
    NP_COMMUNICATION_ID_KEY,
    PLAY_COUNT_KEY,
    PLAY_DURATION_KEY,
    PROGRESS_KEY,
    PS5_NATIVE_GAME_CATEGORY,
    TIER_KEY,
    TITLE_ID_KEY,
    TITLES_KEY,
    TROPHIES_KEY,
    TROPHY_EARNED_RATE_KEY,
    TROPHY_GROUP_ID_KEY,
    TROPHY_GROUP_NAME_KEY,
    TROPHY_GROUPS_KEY,
    TROPHY_ID_KEY,
    TROPHY_LEVEL_KEY,
    TROPHY_NAME_KEY,
    TROPHY_TITLE_NAME_KEY,
    TROPHY_TITLE_PLATFORM_KEY,
    TROPHY_TITLES_KEY,
    TROPHY_TYPE_KEY,
    UNKNOWN_CATEGORY,
    TrophyClient,
    group_trophies_url,
    title_stats_url,
    title_trophy_titles_url,
    trophy_groups_url,
    trophy_summary_url,
    trophy_titles_url,
    user_group_trophies_url,
    user_trophy_groups_url,
)
from test_values import (
    lowercase_token,
    new_account_id,
    new_game_title,
    new_np_communication_id,
    new_percent_completed,
    new_positive_count,
    new_ps4_title_id,
    new_small_count,
    new_trophy_group_id,
    new_utc_instant,
)


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    """Answers by exact URL. A list body is a queue of pages, one popped per call."""

    def __init__(self, *, own_account_id=None, responses=None):
        self._responses = {MY_ACCOUNT_URL: {ACCOUNT_ID_KEY: own_account_id or new_account_id()}, **(responses or {})}
        self.get_calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, headers=None):
        self.get_calls.append((url, params or {}))
        body = self._responses[url]
        return FakeResponse(body.pop(0) if isinstance(body, list) else body)

    async def run_with_reauth(self, operation):
        return await operation()


def _title_entry(name, np_communication_id, platform=PS5):
    return {
        TROPHY_TITLE_NAME_KEY: name,
        NP_COMMUNICATION_ID_KEY: np_communication_id,
        TROPHY_TITLE_PLATFORM_KEY: platform,
    }


async def test_trophy_summary_for_authenticated_user():
    own_account_id = new_account_id()
    expected = TrophySummary(
        level=new_positive_count(),
        progress=new_percent_completed(),
        tier=new_small_count(),
        earned=TrophyCounts(bronze=new_small_count(), gold=new_small_count()),
        account_id=own_account_id,
    )
    body = {
        TROPHY_LEVEL_KEY: expected.level,
        PROGRESS_KEY: expected.progress,
        TIER_KEY: expected.tier,
        EARNED_TROPHIES_KEY: {BRONZE_KEY: expected.earned.bronze, GOLD_KEY: expected.earned.gold},
    }
    session = FakeSession(own_account_id=own_account_id, responses={trophy_summary_url(SELF_PATH_ID): body})

    summary = await TrophyClient(session).trophy_summary()

    assert summary == expected


async def test_trophy_titles_paginates_until_next_offset_is_zero():
    first_name, second_name = new_game_title(), new_game_title()
    pages = [
        {TROPHY_TITLES_KEY: [_title_entry(first_name, new_np_communication_id(), PS5)], NEXT_OFFSET_KEY: 1},
        {TROPHY_TITLES_KEY: [_title_entry(second_name, new_np_communication_id(), PS4)], NEXT_OFFSET_KEY: 0},
    ]
    client = TrophyClient(FakeSession(responses={trophy_titles_url(SELF_PATH_ID): pages}))

    titles = await client.trophy_titles(limit=100)

    assert [(t.name, t.platforms) for t in titles] == [(first_name, (PS5,)), (second_name, (PS4,))]


async def test_trophy_titles_for_title_flattens_nested_entries():
    first_name, second_name = new_game_title(), new_game_title()
    body = {
        TITLES_KEY: [
            {TROPHY_TITLES_KEY: [_title_entry(first_name, new_np_communication_id())]},
            {TROPHY_TITLES_KEY: [_title_entry(second_name, new_np_communication_id())]},
        ]
    }
    client = TrophyClient(FakeSession(responses={title_trophy_titles_url(SELF_PATH_ID): body}))

    titles = await client.trophy_titles_for_title([new_ps4_title_id(), new_ps4_title_id()])

    assert [t.name for t in titles] == [first_name, second_name]


async def test_title_trophies_merges_meta_and_progress():
    np_communication_id = new_np_communication_id()
    trophy_id = new_positive_count()
    expected = TrophyDetail(
        trophy_id=trophy_id,
        name=new_game_title(),
        detail=None,
        type=lowercase_token(),
        earned=True,
        earned_date=new_utc_instant().isoformat(),
        rarity=float(new_percent_completed()),
    )
    meta = {
        TROPHIES_KEY: [
            {
                TROPHY_ID_KEY: trophy_id,
                TROPHY_NAME_KEY: expected.name,
                TROPHY_TYPE_KEY: expected.type,
                TROPHY_EARNED_RATE_KEY: str(expected.rarity),
            }
        ],
        NEXT_OFFSET_KEY: 0,
    }
    progress = {TROPHIES_KEY: [{TROPHY_ID_KEY: trophy_id, EARNED_KEY: True, EARNED_DATE_KEY: expected.earned_date}]}
    group = new_trophy_group_id()
    client = TrophyClient(
        FakeSession(
            responses={
                group_trophies_url(np_communication_id, group): meta,
                user_group_trophies_url(SELF_PATH_ID, np_communication_id, group): progress,
            }
        )
    )

    details = await client.title_trophies(np_communication_id, PS5, group=group)

    assert details == [expected]


async def test_trophy_groups_merges_title_and_group_progress():
    np_communication_id = new_np_communication_id()
    group_id = new_trophy_group_id()
    title_name, group_name = new_game_title(), new_game_title()
    title_progress, group_progress = new_percent_completed(), new_percent_completed()
    earned_gold = new_small_count()
    meta = {
        TROPHY_TITLE_NAME_KEY: title_name,
        TROPHY_GROUPS_KEY: [{TROPHY_GROUP_ID_KEY: group_id, TROPHY_GROUP_NAME_KEY: group_name}],
    }
    progress = {
        TROPHY_TITLE_PLATFORM_KEY: PS5,
        PROGRESS_KEY: title_progress,
        TROPHY_GROUPS_KEY: [
            {TROPHY_GROUP_ID_KEY: group_id, PROGRESS_KEY: group_progress, EARNED_TROPHIES_KEY: {GOLD_KEY: earned_gold}}
        ],
    }
    client = TrophyClient(
        FakeSession(
            responses={
                trophy_groups_url(np_communication_id): meta,
                user_trophy_groups_url(SELF_PATH_ID, np_communication_id): progress,
            }
        )
    )

    result = await client.trophy_groups(np_communication_id, PS5)

    assert result == TrophyGroups(
        title_name=title_name,
        platforms=(PS5,),
        progress=title_progress,
        defined=TrophyCounts(),
        earned=TrophyCounts(),
        groups=(
            TrophyGroup(
                group_id=group_id, name=group_name, progress=group_progress, earned=TrophyCounts(gold=earned_gold)
            ),
        ),
    )


async def test_title_stats_maps_platform_category_and_duration():
    hours, minutes, seconds = random.randint(0, 999), random.randint(0, 59), random.randint(0, 59)
    expected = TitleStat(
        title_id=new_ps4_title_id(),
        name=new_game_title(),
        category=PS5,
        play_count=new_positive_count(),
        play_duration_seconds=hours * 3600 + minutes * 60 + seconds,
    )
    body = {
        TITLES_KEY: [
            {
                TITLE_ID_KEY: expected.title_id,
                NAME_KEY: expected.name,
                CATEGORY_KEY: PS5_NATIVE_GAME_CATEGORY,
                PLAY_COUNT_KEY: expected.play_count,
                PLAY_DURATION_KEY: f"PT{hours}H{minutes}M{seconds}S",
            }
        ],
        NEXT_OFFSET_KEY: 0,
    }
    client = TrophyClient(FakeSession(responses={title_stats_url(SELF_PATH_ID): body}))

    stats = await client.title_stats()

    assert stats == [expected]


async def test_title_stats_unknown_category_falls_back():
    body = {
        TITLES_KEY: [{TITLE_ID_KEY: new_ps4_title_id(), NAME_KEY: new_game_title(), CATEGORY_KEY: lowercase_token()}],
        NEXT_OFFSET_KEY: 0,
    }
    client = TrophyClient(FakeSession(responses={title_stats_url(SELF_PATH_ID): body}))

    stats = await client.title_stats()

    assert stats[0].category == UNKNOWN_CATEGORY
