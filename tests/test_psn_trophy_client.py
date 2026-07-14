"""Tests for TrophyClient, using a hand-written fake session (no network, no credentials).

Ported from ``psnpy``'s ``test_capabilities.py``, split to the trophy-data subset.
"""

from __future__ import annotations

from curator.psn.models import TitleStat, TrophyCounts, TrophyDetail, TrophyGroups, TrophySummary
from curator.psn.trophy_client import TrophyClient


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    """Stands in for a ``curator.psn.session.PsnSession`` instance."""

    def __init__(self, *, own_account_id="123", responses=None):
        self._own_account_id = own_account_id
        self._responses = dict(responses or {})
        self.get_calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, headers=None):
        self.get_calls.append((url, params or {}))
        if "devices/accounts/me" in url:
            return FakeResponse({"accountId": self._own_account_id})
        # Pick the longest (most specific) matching key -- e.g. a "users/me/..." progress-endpoint key
        # must win over a shorter meta-endpoint key that happens to be a substring of the same URL.
        matches = [key for key in self._responses if key in url]
        if matches:
            best = max(matches, key=len)
            return FakeResponse(self._responses[best])
        return FakeResponse({})

    async def run_with_reauth(self, operation):
        return await operation()


async def test_trophy_summary_for_authenticated_user():
    body = {"trophyLevel": 42, "progress": 80, "tier": 4, "earnedTrophies": {"bronze": 1, "gold": 2}}
    client = TrophyClient(FakeSession(own_account_id="999", responses={"trophySummary": body}))

    summary = await client.trophy_summary()

    assert summary == TrophySummary(
        level=42,
        progress=80,
        tier=4,
        earned=TrophyCounts(bronze=1, gold=2),
        account_id="999",
    )


async def test_trophy_titles_paginates_until_next_offset_is_zero():
    page1 = {
        "trophyTitles": [{"trophyTitleName": "Game A", "npCommunicationId": "NPWR1", "trophyTitlePlatform": "PS5"}],
        "nextOffset": 1,
    }
    page2 = {
        "trophyTitles": [{"trophyTitleName": "Game B", "npCommunicationId": "NPWR2", "trophyTitlePlatform": "PS4"}],
        "nextOffset": 0,
    }
    responses = iter([page1, page2])

    class PagingSession(FakeSession):
        async def get(self, url, params=None, headers=None):
            self.get_calls.append((url, params or {}))
            if "trophyTitles" in url:
                return FakeResponse(next(responses))
            return await super().get(url, params, headers)

    client = TrophyClient(PagingSession())

    titles = await client.trophy_titles(limit=100)

    assert [t.name for t in titles] == ["Game A", "Game B"]
    assert titles[0].platforms == ("PS5",)


async def test_trophy_titles_for_title_flattens_nested_entries():
    body = {
        "titles": [
            {"trophyTitles": [{"trophyTitleName": "Game A", "npCommunicationId": "NPWR1"}]},
            {"trophyTitles": [{"trophyTitleName": "Game B", "npCommunicationId": "NPWR2"}]},
        ]
    }
    client = TrophyClient(FakeSession(responses={"titles/trophyTitles": body}))

    titles = await client.trophy_titles_for_title(["NPWR1", "NPWR2"])

    assert [t.name for t in titles] == ["Game A", "Game B"]


async def test_title_trophies_merges_meta_and_progress():
    meta = {
        "trophies": [{"trophyId": 1, "trophyName": "First Blood", "trophyType": "bronze", "trophyEarnedRate": "45.5"}],
        "nextOffset": 0,
    }
    progress = {"trophies": [{"trophyId": 1, "earned": True, "earnedDateTime": "2024-01-01T00:00:00Z"}]}
    client = TrophyClient(
        FakeSession(
            responses={
                "npCommunicationIds/NPWR1/trophyGroups/all/trophies": meta,
                "users/me/npCommunicationIds/NPWR1/trophyGroups/all/trophies": progress,
            }
        )
    )

    details = await client.title_trophies("NPWR1", "PS5")

    assert details == [
        TrophyDetail(
            trophy_id=1,
            name="First Blood",
            detail=None,
            type="bronze",
            hidden=None,
            icon_url=None,
            earned=True,
            earned_date="2024-01-01T00:00:00Z",
            progress_rate=None,
            rarity=45.5,
        )
    ]


async def test_trophy_groups_merges_title_and_group_progress():
    meta = {
        "trophyTitleName": "Game A",
        "trophyGroups": [{"trophyGroupId": "default", "trophyGroupName": "Base Game"}],
    }
    progress = {
        "trophyTitlePlatform": "PS5",
        "progress": 50,
        "trophyGroups": [{"trophyGroupId": "default", "progress": 50, "earnedTrophies": {"gold": 1}}],
    }
    client = TrophyClient(
        FakeSession(
            responses={
                "npCommunicationIds/NPWR1/trophyGroups": meta,
                "users/me/npCommunicationIds/NPWR1/trophyGroups": progress,
            }
        )
    )

    result = await client.trophy_groups("NPWR1", "PS5")

    assert isinstance(result, TrophyGroups)
    assert result.title_name == "Game A"
    assert result.platforms == ("PS5",)
    assert result.groups[0].name == "Base Game"
    assert result.groups[0].earned == TrophyCounts(gold=1)


async def test_title_stats_maps_platform_category_and_duration():
    body = {
        "titles": [
            {
                "titleId": "T1",
                "name": "Game A",
                "category": "ps5_native_game",
                "playCount": 10,
                "playDuration": "PT10H30M15S",
            }
        ],
        "nextOffset": 0,
    }
    client = TrophyClient(FakeSession(responses={"gamelist/v2/users/me/titles": body}))

    stats = await client.title_stats()

    assert stats == [
        TitleStat(
            title_id="T1",
            name="Game A",
            category="PS5",
            play_count=10,
            first_played=None,
            last_played=None,
            play_duration_seconds=10 * 3600 + 30 * 60 + 15,
            image_url=None,
        )
    ]


async def test_title_stats_unknown_category_falls_back():
    body = {"titles": [{"titleId": "T2", "name": "Old Game", "category": "ps3_game"}], "nextOffset": 0}
    client = TrophyClient(FakeSession(responses={"gamelist/v2/users/me/titles": body}))

    stats = await client.title_stats()

    assert stats[0].category == "UNKNOWN"
