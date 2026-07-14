"""Tests for GET /catalog/games, using create_app() with a hand-written fake CatalogRepository.

Reuses test_routes.py's fakes/helpers (FakeRepository, FakeTokenValidator, _claims, _bearer,
_make_settings) the same way test_authz.py does.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.catalog.repository import GameSummary
from curator.persistence.crypto import TokenCrypto
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings


class FakeCatalogRepository:
    def __init__(self, games=None):
        self._games = games or []
        self.list_games_calls = []

    async def list_games(self, *, franchise=None, genre=None, aaa_tier=None, limit=50, offset=0):
        self.list_games_calls.append((franchise, genre, aaa_tier, limit, offset))
        return self._games


def _build(catalog_repository=None):
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
    return TestClient(app), validator


def test_requires_bearer_token():
    client, _validator = _build()

    response = client.get("/catalog/games")

    assert response.status_code == 401


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
        }
    ]


def test_passes_query_filters_through_to_repository():
    catalog_repository = FakeCatalogRepository()
    client, validator = _build(catalog_repository)
    validator.register("token-a", _claims())

    client.get(
        "/catalog/games?franchise=God+of+War&genre=Action&aaaTier=AAA&limit=10&offset=5",
        headers=_bearer("token-a"),
    )

    assert catalog_repository.list_games_calls == [("God of War", "Action", "AAA", 10, 5)]


def test_default_pagination():
    catalog_repository = FakeCatalogRepository()
    client, validator = _build(catalog_repository)
    validator.register("token-a", _claims())

    client.get("/catalog/games", headers=_bearer("token-a"))

    assert catalog_repository.list_games_calls == [(None, None, None, 50, 0)]
