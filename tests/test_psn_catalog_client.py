"""Tests for CatalogClient, using a hand-written fake session (no network, no credentials).

Ported from ``psnpy``'s ``test_capabilities.py``, split to the catalog/search subset.
"""

from __future__ import annotations

from curator.psn.catalog_client import CatalogClient
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
        "contentRating": {"description": "Blood and Gore", "authority": "ESRB"},
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
        release_date="2015-03-24",
        minimum_age=17,
        content_rating="Blood and Gore",
        rating_authority="ESRB",
        star_rating=4.5,
        genres=("Action", "RPG"),
        title_ids=("CUSA00900_00",),
        cover_image_url="master.png",
    )


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
