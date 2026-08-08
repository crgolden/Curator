"""Tests for GET /catalog/games, using create_app() with a hand-written fake CatalogRepository.

Reuses test_routes.py's fakes/helpers (FakeRepository, FakeTokenValidator, _claims, _bearer,
_make_settings) the same way test_authz.py does.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.catalog.repository import GameSummary
from curator.catalog.store_backfill_service import BackfillProgress, BackfillSummary
from curator.persistence.crypto import TokenCrypto
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings


class FakeCatalogRepository:
    def __init__(self, games=None):
        self._games = games or []
        self.list_games_calls = []

    async def list_games(self, *, search=None, franchise=None, genre=None, aaa_tier=None, limit=50, offset=0):
        self.list_games_calls.append((search, franchise, genre, aaa_tier, limit, offset))
        return self._games, len(self._games)


def _build(catalog_repository=None, *, backfill_service=None, omit_backfill_service=False):
    repository = FakeRepository()
    token_crypto = TokenCrypto(Fernet.generate_key())
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
        self._summary = summary or BackfillSummary()

    async def backfill(self, category_ids, *, max_pages_per_category=None):
        self.calls.append((list(category_ids), max_pages_per_category))
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


def test_requires_bearer_token():
    client, _validator = _build()

    response = client.get("/catalog/games")

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
