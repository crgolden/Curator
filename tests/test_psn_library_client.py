"""Tests for LibraryClient, using a hand-written fake session (no network, no credentials).

Ported from ``psnpy``'s ``test_client.py``/``test_capabilities.py``, split to the library-data subset.
"""

from __future__ import annotations

from curator.psn.library_client import LibraryClient
from curator.psn.models import Entitlement, LibraryGame


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    """Stands in for a ``curator.psn.session.PsnSession`` instance."""

    def __init__(self, *, entitlements_pages=None, graphql_responses=None):
        self._entitlements_pages = list(entitlements_pages or [])
        self._graphql_responses = dict(graphql_responses or {})
        self.get_calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, headers=None):
        self.get_calls.append((url, params or {}))
        if "entitlement" in url:
            return FakeResponse(self._entitlements_pages.pop(0) if self._entitlements_pages else {})
        operation_name = (params or {}).get("operationName")
        return FakeResponse(self._graphql_responses.get(operation_name, {}))

    async def run_with_reauth(self, operation):
        return await operation()


async def test_entitlements_maps_fields_and_stops_on_short_page():
    page = {
        "entitlements": [
            {
                "id": "ent-1",
                "gameMeta": {"name": "Bloodborne", "packageType": "PS4GD", "type": "GAME", "iconUrl": "icon.png"},
                "titleMeta": {"titleId": "CUSA00900_00", "name": "Bloodborne (title)", "imageUrl": "title.png"},
                "conceptMeta": {"conceptId": "10000001"},
                "productId": "UP0700-CUSA00900_00-BLOODBORNE0000",
                "activeFlag": True,
                "activeDate": "2021-01-01T00:00:00Z",
            }
        ],
        "totalResults": 1,
    }
    session = FakeSession(entitlements_pages=[page])
    client = LibraryClient(session)

    entitlements = await client.entitlements()

    assert entitlements == [
        Entitlement(
            entitlement_id="ent-1",
            name="Bloodborne",
            title_id="CUSA00900_00",
            concept_id="10000001",
            product_id="UP0700-CUSA00900_00-BLOODBORNE0000",
            package_type="PS4GD",
            game_type="GAME",
            active=True,
            active_date="2021-01-01T00:00:00Z",
            image_url="title.png",
            game_meta_name="Bloodborne",
            concept_meta_name=None,
            title_meta_name="Bloodborne (title)",
        )
    ]


async def test_entitlements_falls_back_to_title_name_and_icon_url():
    page = {
        "entitlements": [
            {
                "id": "ent-2",
                "gameMeta": {"iconUrl": "icon-only.png"},
                "titleMeta": {"name": "Fallback Name"},
                "conceptMeta": {},
                "activeFlag": False,
            }
        ],
        "totalResults": 1,
    }
    client = LibraryClient(FakeSession(entitlements_pages=[page]))

    entitlements = await client.entitlements()

    assert entitlements[0].name == "Fallback Name"
    assert entitlements[0].image_url == "icon-only.png"
    assert entitlements[0].active is False


async def test_entitlements_stops_when_page_is_empty():
    client = LibraryClient(FakeSession(entitlements_pages=[{"entitlements": [], "totalResults": 0}]))

    assert await client.entitlements() == []


def _synthetic_page(start_id, count, *, total_results):
    return {
        "entitlements": [
            {"id": f"ent-{start_id + i}", "gameMeta": {"name": f"Game {start_id + i}"}} for i in range(count)
        ],
        "totalResults": total_results,
    }


async def test_entitlements_default_is_unbounded_past_500():
    """Regression test: entitlements() must not silently cap at 500 -- a real PSN library can easily
    exceed that (see the ~845-title account that exposed this bug), and this endpoint has no documented
    rate limit, so there is no reason to stop early. Builds 26 full pages of 20 (page_size is fixed at 20)
    plus one partial page, totalling 523 entries -- more than a would-be 500 cap could ever return -- and
    asserts the default call (no ``limit`` argument) fetches every one of them.
    """
    total = 523
    full_pages, remainder = divmod(total, 20)
    pages = [_synthetic_page(i * 20, 20, total_results=total) for i in range(full_pages)]
    if remainder:
        pages.append(_synthetic_page(full_pages * 20, remainder, total_results=total))
    client = LibraryClient(FakeSession(entitlements_pages=pages))

    entitlements = await client.entitlements()

    assert len(entitlements) == total


async def test_entitlements_explicit_limit_still_caps():
    # PSN honors the requested page limit, so a single page of exactly 15 (page_limit = min(20, 15)) is
    # what a real response would look like -- proves an explicit limit still stops the loop early even
    # though totalResults says there's far more available.
    page = _synthetic_page(0, 15, total_results=1000)
    client = LibraryClient(FakeSession(entitlements_pages=[page]))

    entitlements = await client.entitlements(limit=15)

    assert len(entitlements) == 15


async def test_recently_played_maps_games():
    graphql_responses = {
        "getUserGameList": {
            "data": {
                "gameLibraryTitlesRetrieve": {
                    "games": [
                        {
                            "titleId": "CUSA00419_00",
                            "name": "Horizon Zero Dawn",
                            "platform": "PS4",
                            "conceptId": "10001",
                            "productId": "UP9000-CUSA00419_00",
                            "image": {"url": "cover.png"},
                            "lastPlayedDateTime": "2024-01-01T00:00:00Z",
                            "isActive": True,
                        }
                    ]
                }
            }
        }
    }
    client = LibraryClient(FakeSession(graphql_responses=graphql_responses))

    games = await client.recently_played()

    assert games == [
        LibraryGame(
            title_id="CUSA00419_00",
            name="Horizon Zero Dawn",
            platform="PS4",
            concept_id="10001",
            product_id="UP9000-CUSA00419_00",
            image_url="cover.png",
            last_played="2024-01-01T00:00:00Z",
            is_active=True,
        )
    ]


async def test_recently_played_joins_multi_platform_list():
    graphql_responses = {
        "getUserGameList": {
            "data": {
                "gameLibraryTitlesRetrieve": {
                    "games": [{"titleId": "T1", "name": "Cross-Gen Game", "platform": ["PS4", "PS5"]}]
                }
            }
        }
    }
    client = LibraryClient(FakeSession(graphql_responses=graphql_responses))

    games = await client.recently_played()

    assert games[0].platform == "PS4/PS5"


async def test_purchased_maps_games():
    graphql_responses = {
        "getPurchasedGameList": {
            "data": {"purchasedTitlesRetrieve": {"games": [{"titleId": "T2", "name": "Purchased Game"}]}}
        }
    }
    client = LibraryClient(FakeSession(graphql_responses=graphql_responses))

    games = await client.purchased()

    assert games[0].title_id == "T2"
    assert games[0].name == "Purchased Game"


async def test_recently_played_empty_when_no_games():
    client = LibraryClient(FakeSession(graphql_responses={"getUserGameList": {"data": {}}}))

    assert await client.recently_played() == []
