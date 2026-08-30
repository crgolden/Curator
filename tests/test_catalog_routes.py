"""Tests for GET /catalog/games, using create_app() with a hand-written fake CatalogRepository.

Reuses test_routes.py's fakes/helpers (FakeRepository, FakeTokenValidator, _claims, _bearer,
_make_settings) the same way test_authz.py does.
"""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.catalog.repository import CatalogRepository, GameSummary
from curator.catalog.store_backfill_service import BackfillProgress, BackfillSummary
from curator.persistence.crypto import TokenCrypto
from curator.psn.store_client import StoreCatalogClient
from test_catalog_repository import FakePool
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings

MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS = (
    "SHOOTER",
    "FIGHTING",
    "ROLE_PLAYING_GAMES",
    "SPORTS",
    "RACING",
    "PUZZLE",
    "MUSIC/RHYTHM",
    "HORROR",
    "STRATEGY",
    "QUIZ",
    "BRAIN_TRAINING",
    "EDUCATIONAL",
    "FITNESS",
    "PARTY",
    "ADULT",
    "SIMULATOR",
    "SIMULATION",
    "ARCADE",
    "ADVENTURE",
    "FAMILY",
    "CASUAL",
    "ACTION",
    "UNIQUE",
)


def _genre_facet_response(keys):
    return {
        "data": {
            "categoryGridRetrieve": {
                "products": [],
                "pageInfo": {"totalCount": len(keys), "offset": 0, "size": 1, "isLast": False},
                "facetOptions": [
                    {"name": "productGenres", "values": [{"key": key, "count": 1} for key in keys]},
                ],
            }
        }
    }


def _store_client(payload):
    def handler(_request):
        return httpx.Response(200, json=payload)

    return StoreCatalogClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class FakeCatalogRepository:
    def __init__(self, games=None, genres=None, genre_vocabulary=None):
        self._games = games or []
        self._genres = genres or []
        self._genre_vocabulary = list(genre_vocabulary or [])
        self.list_games_calls = []
        self.get_game_calls = []

    async def list_genre_vocabulary(self):
        return list(self._genre_vocabulary)

    async def list_games(self, *, search=None, franchise=None, genre=None, aaa_tier=None, limit=50, offset=0):
        self.list_games_calls.append((search, franchise, genre, aaa_tier, limit, offset))
        return self._games, len(self._games)

    async def get_game(self, game_id, identity_sub=None):
        self.get_game_calls.append((game_id, identity_sub))
        return next((game for game in self._games if game.game_id == game_id), None)

    async def list_genres(self):
        return list(self._genres)


def _build(
    catalog_repository=None,
    *,
    backfill_service=None,
    omit_backfill_service=False,
    store_client=None,
    omit_store_client=False,
):
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
    if omit_store_client:
        app.state.store_catalog_client = None
    elif store_client is not None:
        app.state.store_catalog_client = store_client
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


def test_genre_drift_reports_a_live_facet_key_the_genres_table_has_never_heard_of():
    unseeded_facet_key = "METAVERSE"
    client, validator = _build(
        FakeCatalogRepository(genre_vocabulary=MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS),
        store_client=_store_client(
            _genre_facet_response((*MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS, unseeded_facet_key))
        ),
    )
    validator.register("admin-token", _claims(is_admin=True))

    response = client.get("/catalog/genres/drift", headers=_bearer("admin-token"))

    assert response.status_code == 200
    body = response.json()
    assert body["missing_from_table"] == [unseeded_facet_key]
    assert body["missing_from_facet"] == []
    assert body["matched"] == len(MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS)


def test_genre_drift_reports_nothing_when_the_facet_matches_the_seeded_vocabulary_exactly():
    client, validator = _build(
        FakeCatalogRepository(genre_vocabulary=MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS),
        store_client=_store_client(_genre_facet_response(MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS)),
    )
    validator.register("admin-token", _claims(is_admin=True))

    body = client.get("/catalog/genres/drift", headers=_bearer("admin-token")).json()

    assert body["missing_from_table"] == []
    assert body["missing_from_facet"] == []
    assert body["matched"] == len(MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS)


def test_genre_drift_reports_a_seeded_genre_the_storefront_has_stopped_publishing():
    retired_genre = "BRAIN_TRAINING"
    still_published = tuple(key for key in MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS if key != retired_genre)
    client, validator = _build(
        FakeCatalogRepository(genre_vocabulary=MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS),
        store_client=_store_client(_genre_facet_response(still_published)),
    )
    validator.register("admin-token", _claims(is_admin=True))

    body = client.get("/catalog/genres/drift", headers=_bearer("admin-token")).json()

    assert body["missing_from_facet"] == [retired_genre]
    assert body["missing_from_table"] == []


def test_genre_drift_writes_nothing_at_all_to_the_database():
    pool = FakePool(fetchall_results=[[(key,) for key in MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS]])
    client, validator = _build(
        CatalogRepository(pool),
        store_client=_store_client(_genre_facet_response(MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS)),
    )
    validator.register("admin-token", _claims(is_admin=True))

    client.get("/catalog/genres/drift", headers=_bearer("admin-token"))

    executed = [sql for conn in pool.connections for sql, _params in conn.executed]
    assert executed, "the detector must actually have read the genres table"
    assert all(sql.strip().startswith("SELECT") for sql in executed), executed


def test_genre_drift_rejects_a_category_that_publishes_no_product_genres_facet():
    no_genre_facet = {
        "data": {
            "categoryGridRetrieve": {
                "products": [],
                "pageInfo": {"totalCount": 143, "offset": 0, "size": 1, "isLast": False},
                "facetOptions": [{"name": "targetPlatforms", "values": [{"key": "PS5", "count": 143}]}],
            }
        }
    }
    client, validator = _build(
        FakeCatalogRepository(genre_vocabulary=MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS),
        store_client=_store_client(no_genre_facet),
    )
    validator.register("admin-token", _claims(is_admin=True))

    response = client.get("/catalog/genres/drift?categoryId=cat-with-no-genres", headers=_bearer("admin-token"))

    assert response.status_code == 502, "an empty delta here would read as 'no drift' when nothing was compared"


def test_genre_drift_requires_admin_not_merely_a_bearer_token():
    client, validator = _build(
        store_client=_store_client(_genre_facet_response(MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS))
    )
    validator.register("token-a", _claims())

    assert client.get("/catalog/genres/drift", headers=_bearer("token-a")).status_code == 403


def test_genre_drift_is_not_anonymous_like_the_rest_of_the_catalog_routes():
    client, _validator = _build(
        store_client=_store_client(_genre_facet_response(MIGRATION_0036_SEEDED_PRODUCT_GENRE_FACET_KEYS))
    )

    assert client.get("/catalog/genres/drift").status_code == 401


def test_genre_drift_returns_503_when_the_store_client_is_not_configured():
    client, validator = _build(omit_store_client=True)
    validator.register("admin-token", _claims(is_admin=True))

    assert client.get("/catalog/genres/drift", headers=_bearer("admin-token")).status_code == 503


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


def test_an_all_empty_backfill_422s_but_still_returns_what_each_category_did():
    """A bare detail string tells the caller the request failed and nothing about which id was wrong or
    how far each walk got. The 422 carries the same per-category rows a 2xx would."""
    summary = BackfillSummary(
        categories=[
            _progress("cat-1", pages_read=1, products_seen=0, games_created=0, stopped_reason="no_products"),
            _progress("cat-2", pages_read=1, products_seen=0, games_created=0, stopped_reason="no_products"),
        ]
    )
    client, validator = _build(backfill_service=FakeBackfillService(summary))
    validator.register("admin-token", _claims(is_admin=True))

    response = client.post(
        "/catalog/backfill",
        json={"category_ids": ["cat-1", "cat-2"]},
        headers=_bearer("admin-token"),
    )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "empty grid" in detail["message"]
    assert [row["category_id"] for row in detail["categories"]] == ["cat-1", "cat-2"]
    assert all(row["stopped_reason"] == "no_products" for row in detail["categories"])
    assert detail["categories"][0]["pages_read"] == 1


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
