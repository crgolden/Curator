"""Tests for CatalogClient, using a hand-written fake session (no network, no credentials).

Ported from ``psnpy``'s ``test_capabilities.py``, split to the catalog/search subset.
"""

from __future__ import annotations

from datetime import date

import pytest

from curator.psn.catalog_client import CatalogClient, InMemoryTokenStore, RotatingCatalogClient
from curator.psn.errors import PsnAuthError
from curator.psn.models import GameSearchResult, TitleConcept


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    """Stands in for a ``curator.psn.session.PsnSession`` instance."""

    def __init__(self, *, concept_details=None, context_response=None, domain_responses=None):
        self._concept_details = concept_details
        self._context_response = context_response or {}
        self._domain_responses = list(domain_responses or [])
        self.get_calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, headers=None):
        self.get_calls.append((url, params or {}))
        if "/concepts" in url:
            return FakeResponse(self._concept_details if self._concept_details is not None else [])
        operation_name = (params or {}).get("operationName")
        if operation_name == "metGetContextSearchResults":
            return FakeResponse(self._context_response)
        if operation_name == "metGetDomainSearchResults":
            return FakeResponse(self._domain_responses.pop(0) if self._domain_responses else {})
        return FakeResponse({})

    async def run_with_reauth(self, operation):
        return await operation()


async def test_title_concept_maps_fields():
    concept = {
        "id": "10000001",
        "name": "Bloodborne",
        "type": "GAME",
        "publisherName": "Sony Interactive Entertainment",
        "releaseDate": {"date": "2015-03-24"},
        "minimumAge": 17,
        "contentRating": {"name": "ESRB_MATURE_17", "description": "ESRB Mature 17+", "authority": "ESRB"},
        "starRating": {"score": "4.5"},
        "genres": ["Action", "RPG"],
        "titleIds": ["CUSA00900_00"],
        "media": {"images": [{"type": "MASTER", "url": "master.png"}]},
    }
    client = CatalogClient(FakeSession(concept_details=[concept]))

    result = await client.title_concept("CUSA00900_00")

    assert result == TitleConcept(
        concept_id="10000001",
        name="Bloodborne",
        type="GAME",
        publisher="Sony Interactive Entertainment",
        release_date=date(2015, 3, 24),
        minimum_age=17,
        content_rating="ESRB_MATURE_17",
        rating_authority="ESRB",
        star_rating=4.5,
        genres=("Action", "RPG"),
        title_ids=("CUSA00900_00",),
        cover_image_url="master.png",
    )


async def test_title_concept_drops_the_time_from_psns_full_release_timestamp():
    concept = {"id": "1", "releaseDate": {"date": "2018-10-05T04:00:00Z"}}
    client = CatalogClient(FakeSession(concept_details=[concept]))

    result = await client.title_concept("CUSA00900_00")

    assert result.release_date == date(2018, 10, 5)


async def test_title_concept_leaves_release_date_none_when_psn_sends_something_unparseable():
    concept = {"id": "1", "releaseDate": {"date": "coming soon"}}
    client = CatalogClient(FakeSession(concept_details=[concept]))

    result = await client.title_concept("CUSA00900_00")

    assert result.release_date is None


async def test_title_concept_leaves_release_date_none_when_psn_omits_it():
    client = CatalogClient(FakeSession(concept_details=[{"id": "1"}]))

    result = await client.title_concept("CUSA00900_00")

    assert result.release_date is None


async def test_title_concept_reads_multiplayer_from_psns_network_player_count():
    concept = {
        "id": "1",
        "compatibilityNotices": [
            {"type": "NO_OF_PLAYERS", "value": "1"},
            {"type": "REMOTE_PLAY_SUPPORTED", "value": "true"},
            {"type": "NO_OF_NETWORK_PLAYERS", "value": "64"},
        ],
    }
    client = CatalogClient(FakeSession(concept_details=[concept]))

    result = await client.title_concept("CUSA00900_00")

    assert result.multiplayer is True


async def test_title_concept_reads_single_player_from_psns_local_player_count():
    concept = {"id": "1", "compatibilityNotices": [{"type": "NO_OF_PLAYERS", "value": "1"}]}
    client = CatalogClient(FakeSession(concept_details=[concept]))

    result = await client.title_concept("CUSA00900_00")

    assert result.multiplayer is False


async def test_title_concept_reports_no_multiplayer_opinion_when_psn_publishes_no_player_count():
    concept = {"id": "1", "compatibilityNotices": [{"type": "REMOTE_PLAY_SUPPORTED", "value": "true"}]}
    client = CatalogClient(FakeSession(concept_details=[concept]))

    result = await client.title_concept("CUSA00900_00")

    assert result.multiplayer is None


async def test_title_concept_prefers_gamehub_cover_art_over_master():
    concept = {
        "id": "1",
        "media": {
            "images": [
                {"type": "MASTER", "url": "master.png"},
                {"type": "GAMEHUB_COVER_ART", "url": "cover.png"},
            ]
        },
    }
    client = CatalogClient(FakeSession(concept_details=[concept]))

    result = await client.title_concept("T1")

    assert result.cover_image_url == "cover.png"


async def test_title_concept_returns_empty_concept_when_no_details():
    client = CatalogClient(FakeSession(concept_details=[]))

    result = await client.title_concept("UNKNOWN")

    assert result == TitleConcept(concept_id=None)


async def test_search_games_maps_first_page_from_context_query():
    context_response = {
        "data": {
            "universalContextSearch": {
                "results": [
                    {
                        "searchResults": [
                            {
                                "result": {
                                    "id": "CUSA00900_00",
                                    "name": "Bloodborne",
                                    "type": "GAME",
                                    "platforms": ["PS4"],
                                    "price": {"basePrice": "$19.99", "discountedPrice": "$9.99", "isFree": False},
                                    "media": [{"url": "thumb.png"}],
                                }
                            }
                        ],
                        "next": "",
                    }
                ]
            }
        }
    }
    client = CatalogClient(FakeSession(context_response=context_response))

    results = await client.search_games("bloodborne")

    assert results == [
        GameSearchResult(
            id="CUSA00900_00",
            name="Bloodborne",
            type="GAME",
            platforms=("PS4",),
            image_url="thumb.png",
            price="$19.99",
            discounted_price="$9.99",
            is_free=False,
        )
    ]


class FakeRotationClient:
    def __init__(self, name, *, rejects=False, raises=None):
        self.name = name
        self._rejects = rejects
        self._raises = raises
        self.calls = 0

    async def title_concept(self, title_id, platform="PS5"):
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        if self._rejects:
            raise PsnAuthError(f"{self.name} rejected")
        return TitleConcept(concept_id=self.name)


async def test_rotating_catalog_client_advances_past_a_rejected_account():
    first = FakeRotationClient("first", rejects=True)
    second = FakeRotationClient("second")
    client = RotatingCatalogClient([first, second])

    result = await client.title_concept("CUSA00900_00")

    assert result.concept_id == "second"
    assert (first.calls, second.calls) == (1, 1)


async def test_rotating_catalog_client_stays_on_the_working_account_for_later_calls():
    first = FakeRotationClient("first", rejects=True)
    second = FakeRotationClient("second")
    client = RotatingCatalogClient([first, second])

    await client.title_concept("CUSA00900_00")
    await client.title_concept("CUSA00901_00")

    assert first.calls == 1, "a rejected account must not be retried once rotation has moved past it"
    assert second.calls == 2


async def test_rotating_catalog_client_raises_the_last_rejection_when_every_account_fails():
    clients = [FakeRotationClient("first", rejects=True), FakeRotationClient("second", rejects=True)]
    client = RotatingCatalogClient(clients)

    with pytest.raises(PsnAuthError, match="second rejected"):
        await client.title_concept("CUSA00900_00")

    assert [c.calls for c in clients] == [1, 1], "each account is tried exactly once per call"


async def test_rotating_catalog_client_does_not_rotate_on_a_non_auth_failure():
    first = FakeRotationClient("first", raises=RuntimeError("transport blew up"))
    second = FakeRotationClient("second")
    client = RotatingCatalogClient([first, second])

    with pytest.raises(RuntimeError, match="transport blew up"):
        await client.title_concept("CUSA00900_00")

    assert second.calls == 0, "only a rejected credential is a reason to burn another account"


async def test_rotating_catalog_client_rejects_an_empty_client_list():
    with pytest.raises(ValueError, match="at least one client"):
        RotatingCatalogClient([])


async def test_in_memory_token_store_round_trips_without_touching_a_database():
    store = InMemoryTokenStore()

    assert await store.load() is None

    await store.save({"access_token": "abc"})
    assert await store.load() == {"access_token": "abc"}

    await store.clear()
    assert await store.load() is None


async def test_search_games_paginates_via_domain_query_until_limit():
    context_response = {
        "data": {
            "universalContextSearch": {
                "results": [
                    {"searchResults": [{"result": {"id": "G1", "name": "Game One"}}], "next": "cursor-1"},
                ]
            }
        }
    }
    domain_page = {
        "data": {
            "universalDomainSearch": {
                "searchResults": [{"result": {"id": "G2", "name": "Game Two"}}],
                "next": "",
            }
        }
    }
    client = CatalogClient(FakeSession(context_response=context_response, domain_responses=[domain_page]))

    results = await client.search_games("game", limit=2)

    assert [r.id for r in results] == ["G1", "G2"]


async def test_search_games_stops_when_domain_page_is_empty():
    context_response = {
        "data": {
            "universalContextSearch": {
                "results": [
                    {"searchResults": [{"result": {"id": "G1", "name": "Game One"}}], "next": "cursor-1"},
                ]
            }
        }
    }
    domain_page = {"data": {"universalDomainSearch": {"searchResults": [], "next": ""}}}
    client = CatalogClient(FakeSession(context_response=context_response, domain_responses=[domain_page]))

    results = await client.search_games("game", limit=5)

    assert [r.id for r in results] == ["G1"]
