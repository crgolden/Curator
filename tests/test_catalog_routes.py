"""Tests for GET /catalog/games, using create_app() with a hand-written fake CatalogRepository.

Reuses test_routes.py's fakes/helpers (FakeRepository, FakeTokenValidator, _claims, _bearer,
_make_settings) the same way test_authz.py does.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from curator.app import create_app
from curator.catalog.repository import GameSummary
from curator.catalog.store_backfill_service import BackfillProgress, BackfillSummary
from curator.persistence.crypto import TokenCrypto
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings


class FakeCatalogRepository:
    def __init__(self, games=None, genres=None):
        self._games = games or []
        self._genres = genres or []
        self.list_games_calls = []
        self.get_game_calls = []

    async def list_games(self, *, search=None, franchise=None, genre=None, aaa_tier=None, limit=50, offset=0):
        self.list_games_calls.append((search, franchise, genre, aaa_tier, limit, offset))
        return self._games, len(self._games)

    async def get_game(self, game_id, identity_sub=None):
        self.get_game_calls.append((game_id, identity_sub))
        return next((game for game in self._games if game.game_id == game_id), None)

    async def list_genres(self):
        return list(self._genres)


def _build(catalog_repository=None, *, backfill_service=None, omit_backfill_service=False):
    repository = FakeRepository()
    token_crypto = TokenCrypto(TokenCrypto.generate_key())
    validator = FakeTokenValidator()
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=validator,
        catalog_repository=catalog_repository or FakeCatalogRepository(),
    )
    if omit_backfill_service:
        app.state.store_backfill_service = None
    elif backfill_service is not None:
        app.state.store_backfill_service = backfill_service
    return TestClient(app), validator


class FakeBackfillService:
    def __init__(self, summary=None):
        self.calls: list[tuple[list[str], int | None]] = []
        self.start_offsets: list[dict[str, int]] = []
        self._summary = summary or BackfillSummary()

    async def backfill(self, category_ids, *, max_pages_per_category=None, start_offsets=None):
        self.calls.append((list(category_ids), max_pages_per_category))
        self.start_offsets.append(dict(start_offsets or {}))
        return self._summary


def _progress(category_id="cat-1", **overrides):
    fields = {
        "category_id": category_id,
        "next_offset": 100,
        "completed": False,
        "pages_read": 1,
        "products_seen": 100,
        "games_created": 12,
        "covers_cached": 90,
        "stopped_reason": None,
    }
    fields.update(overrides)
    return BackfillProgress(**fields)


def test_browsing_the_catalog_needs_no_token():
    catalog_repository = FakeCatalogRepository(
        [GameSummary(game_id="g1", canonical_title="Bloodborne", franchise=None, genre="Action", aaa_tier="AAA")]
    )
    client, _validator = _build(catalog_repository)

    response = client.get("/catalog/games")

    assert response.status_code == 200
    assert response.json()["games"][0]["canonical_title"] == "Bloodborne"


def test_catalog_carries_ratings_and_the_derived_tier():
    catalog_repository = FakeCatalogRepository(
        [
            GameSummary(
                game_id="g1",
                canonical_title="Bloodborne",
                franchise=None,
                genre="Action",
                aaa_tier="AAA",
                critical_score=92.0,
                oc_score=91.5,
                psn_rating=4.7,
            )
        ]
    )
    client, _validator = _build(catalog_repository)

    game = client.get("/catalog/games").json()["games"][0]

    assert game["critical_score"] == 92.0
    assert game["oc_score"] == 91.5
    assert game["psn_rating"] == 4.7
    assert game["aaa_tier"] == "AAA"


def test_listing_the_genre_filter_options_needs_no_token():
    client, _validator = _build(FakeCatalogRepository(genres=["Shooter", "RPG"]))

    response = client.get("/catalog/genres")

    assert response.status_code == 200
    assert response.json()["genres"] == ["Shooter", "RPG"]


def test_genres_keep_the_curation_priority_order_rather_than_being_sorted_alphabetically():
    client, _validator = _build(FakeCatalogRepository(genres=["Shooter", "RPG", "Adventure", "Action"]))

    assert client.get("/catalog/genres").json()["genres"] == ["Shooter", "RPG", "Adventure", "Action"]


def test_a_catalog_with_no_enriched_games_offers_no_genres_rather_than_erroring():
    client, _validator = _build(FakeCatalogRepository(genres=[]))

    response = client.get("/catalog/genres")

    assert response.status_code == 200
    assert response.json()["genres"] == []


def test_backfill_still_requires_admin_now_that_browsing_is_anonymous():
    client, _validator = _build()

    response = client.post("/catalog/backfill", json={"category_ids": ["cat-1"]})

    assert response.status_code == 401


def test_backfill_requires_admin_not_merely_a_bearer_token():
    client, validator = _build()
    validator.register("token-a", _claims())

    response = client.post("/catalog/backfill", json={"category_ids": ["cat-1"]}, headers=_bearer("token-a"))

    assert response.status_code == 403


def test_backfill_reports_progress_and_totals():
    summary = BackfillSummary(
        categories=[
            _progress("cat-1", completed=True, games_created=12, covers_cached=90),
            _progress(
                "cat-2", completed=False, games_created=3, covers_cached=3, stopped_reason="page_budget_exhausted"
            ),
        ]
    )
    service = FakeBackfillService(summary)
    client, validator = _build(backfill_service=service)
    validator.register("admin-token", _claims(is_admin=True))

    response = client.post(
        "/catalog/backfill",
        json={"category_ids": ["cat-1", "cat-2"], "max_pages_per_category": 5},
        headers=_bearer("admin-token"),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["completed"] is False, "one unfinished category means the run is unfinished"
    assert body["games_created"] == 15
    assert body["covers_cached"] == 93
    assert body["categories"][1]["stopped_reason"] == "page_budget_exhausted"
    assert body["categories"][1]["next_offset"] == 100, "the caller resumes from here"
    assert service.calls == [(["cat-1", "cat-2"], 5)]


def test_reading_one_game_needs_no_token():
    catalog_repository = FakeCatalogRepository(
        [
            GameSummary(
                game_id="g1",
                canonical_title="Bloodborne",
                franchise=None,
                genre="Action",
                aaa_tier="AAA",
                psn_rating=4.7,
            )
        ]
    )
    client, _validator = _build(catalog_repository)

    response = client.get("/catalog/games/g1")

    assert response.status_code == 200
    body = response.json()
    assert body["canonical_title"] == "Bloodborne"
    assert body["psn_rating"] == 4.7


def test_an_anonymous_visitor_gets_no_trophy_progress_on_a_game_page():
    catalog_repository = FakeCatalogRepository(
        [GameSummary(game_id="g1", canonical_title="Bloodborne", franchise=None, genre="RPG", aaa_tier="AAA")]
    )
    client, _validator = _build(catalog_repository)

    response = client.get("/catalog/games/g1")

    assert response.status_code == 200
    assert response.json()["percent_completed"] is None
    assert catalog_repository.get_game_calls == [("g1", None)]


def test_a_signed_in_caller_gets_their_own_trophy_progress_on_a_game_page():
    catalog_repository = FakeCatalogRepository(
        [
            GameSummary(
                game_id="g1",
                canonical_title="Bloodborne",
                franchise=None,
                genre="RPG",
                aaa_tier="AAA",
                percent_completed=64,
            )
        ]
    )
    client, validator = _build(catalog_repository)
    claims = _claims()
    validator.register("token-a", claims)

    response = client.get("/catalog/games/g1", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json()["percent_completed"] == 64
    assert catalog_repository.get_game_calls == [("g1", claims.sub)]


def test_a_game_page_rejects_a_supplied_token_that_is_invalid_rather_than_serving_it_anonymously():
    catalog_repository = FakeCatalogRepository(
        [GameSummary(game_id="g1", canonical_title="Bloodborne", franchise=None, genre="RPG", aaa_tier="AAA")]
    )
    client, _validator = _build(catalog_repository)

    response = client.get("/catalog/games/g1", headers=_bearer("not-a-real-token"))

    assert response.status_code == 401


def test_reading_an_unknown_game_is_a_404_not_an_empty_body():
    client, _validator = _build(FakeCatalogRepository([]))

    response = client.get("/catalog/games/missing")

    assert response.status_code == 404


def test_backfill_resumes_a_category_from_the_offset_a_previous_run_reported():
    service = FakeBackfillService()
    client, validator = _build(backfill_service=service)
    validator.register("admin-token", _claims(is_admin=True))

    client.post(
        "/catalog/backfill",
        json={"category_ids": ["cat-1"], "start_offsets": {"cat-1": 1200}},
        headers=_bearer("admin-token"),
    )

    assert service.start_offsets == [{"cat-1": 1200}]


def test_backfill_starts_from_zero_when_no_offset_is_given():
    service = FakeBackfillService()
    client, validator = _build(backfill_service=service)
    validator.register("admin-token", _claims(is_admin=True))

    client.post("/catalog/backfill", json={"category_ids": ["cat-1"]}, headers=_bearer("admin-token"))

    assert service.start_offsets == [{}]


def test_backfill_returns_503_when_the_store_client_is_not_configured():
    client, validator = _build(backfill_service=None, omit_backfill_service=True)
    validator.register("admin-token", _claims(is_admin=True))

    response = client.post("/catalog/backfill", json={"category_ids": ["cat-1"]}, headers=_bearer("admin-token"))

    assert response.status_code == 503


def test_returns_games_from_repository():
    games = [
        GameSummary(game_id="g1", canonical_title="God of War", franchise="God of War", genre="Action", aaa_tier="AAA")
    ]
    catalog_repository = FakeCatalogRepository(games=games)
    client, validator = _build(catalog_repository)
    validator.register("token-a", _claims())

    response = client.get("/catalog/games", headers=_bearer("token-a"))

    assert response.status_code == 200
    body = response.json()
    assert body["games"] == [
        {
            "game_id": "g1",
            "canonical_title": "God of War",
            "franchise": "God of War",
            "genre": "Action",
            "aaa_tier": "AAA",
            "cover_image_url": None,
            "store_product_id": None,
            "critical_score": None,
            "oc_score": None,
            "psn_rating": None,
            "percent_completed": None,
        }
    ]
    assert body["total"] == 1


def test_passes_query_filters_through_to_repository():
    catalog_repository = FakeCatalogRepository()
    client, validator = _build(catalog_repository)
    validator.register("token-a", _claims())

    client.get(
        "/catalog/games?franchise=God+of+War&genre=Action&aaaTier=AAA&limit=10&offset=5",
        headers=_bearer("token-a"),
    )

    assert catalog_repository.list_games_calls == [(None, "God of War", "Action", "AAA", 10, 5)]


def test_title_search_reaches_the_repository():
    catalog_repository = FakeCatalogRepository()
    client, validator = _build(catalog_repository)
    validator.register("token-a", _claims())

    client.get("/catalog/games?q=tomb", headers=_bearer("token-a"))

    assert catalog_repository.list_games_calls == [("tomb", None, None, None, 50, 0)]


def test_default_pagination():
    catalog_repository = FakeCatalogRepository()
    client, validator = _build(catalog_repository)
    validator.register("token-a", _claims())

    client.get("/catalog/games", headers=_bearer("token-a"))

    assert catalog_repository.list_games_calls == [(None, None, None, None, 50, 0)]
