"""Tests for GET /public/collections/{share_slug} -- the one anonymous, unauthenticated route in this
API. No Authorization header is ever sent in these tests; that omission is the point.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from curator.app import create_app
from curator.collections.repository import CollectionDefinition, CollectionItem
from curator.persistence.crypto import TokenCrypto
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _make_settings


class FakeCollectionsRepository:
    def __init__(self, definitions=None, items_by_definition=None):
        self._by_slug = {d.share_slug: d for d in (definitions or []) if d.share_slug is not None}
        self._items = items_by_definition or {}

    async def get_definition_by_share_slug(self, share_slug):
        definition = self._by_slug.get(share_slug)
        if definition is None or definition.visibility == "private":
            return None
        return definition

    async def list_definition_items(self, definition_id):
        return self._items.get(definition_id, [])


def _definition(definition_id="def-a", visibility="public", share_slug="abc123"):
    return CollectionDefinition(
        definition_id=definition_id,
        identity_sub="sub-a",
        name="A's shared list",
        kind="filter_list",
        console_id=None,
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
        description="curated",
        visibility=visibility,
        share_slug=share_slug,
    )


def _item(game_id="g1"):
    return CollectionItem(
        game_id=game_id,
        rank=1,
        title="Elden Ring",
        franchise=None,
        genre="RPG",
        aaa_tier="AAA",
        critical_score=95.0,
        oc_score=90.0,
        psn_rating=4.8,
        cover_image_url="elden-ring.png",
        owner_has_access=True,
    )


def _build(collections_repository=None):
    repository = FakeRepository()
    token_crypto = TokenCrypto(TokenCrypto.generate_key())
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=FakeTokenValidator(),
        collections_repository=collections_repository or FakeCollectionsRepository(),
    )
    return TestClient(app)


def test_returns_a_public_collection_with_no_auth_header_at_all():
    repo = FakeCollectionsRepository([_definition(visibility="public")], {"def-a": [_item()]})
    client = _build(repo)

    response = client.get("/public/collections/abc123")

    assert response.status_code == 200
    body = response.json()
    assert body["definition_id"] == "def-a"
    assert body["name"] == "A's shared list"
    assert body["visibility"] == "public"
    assert len(body["items"]) == 1
    assert body["items"][0]["game_id"] == "g1"


def test_returns_an_unlisted_collection_too():
    repo = FakeCollectionsRepository([_definition(visibility="unlisted")])
    client = _build(repo)

    response = client.get("/public/collections/abc123")

    assert response.status_code == 200
    assert response.json()["visibility"] == "unlisted"


def test_a_private_collections_slug_404s_exactly_like_an_unknown_one():
    repo = FakeCollectionsRepository([_definition(visibility="private")])
    client = _build(repo)

    response = client.get("/public/collections/abc123")

    assert response.status_code == 404


def test_unknown_slug_404s():
    client = _build()

    response = client.get("/public/collections/does-not-exist")

    assert response.status_code == 404


def test_response_omits_authoring_fields_an_anonymous_viewer_should_not_see():

    repo = FakeCollectionsRepository([_definition()])
    client = _build(repo)

    response = client.get("/public/collections/abc123")

    body = response.json()
    assert "console_id" not in body
    assert "genre_filter" not in body
    assert "min_percent_completed" not in body
