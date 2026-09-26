"""Tests for GET /public/collections/{share_slug} -- the one anonymous, unauthenticated route in this
API. No Authorization header is ever sent in these tests; that omission is the point.
"""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient

from curator import public_collections_routes
from curator.app import create_app
from curator.collections.collection_spec import FILTER_LIST_KIND
from curator.collections.repository import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    VISIBILITY_UNLISTED,
    CollectionDefinition,
    CollectionItem,
)
from curator.persistence.crypto import TokenCrypto
from curator.public_collections_routes import PublicCollectionResponse
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _make_settings, _path
from test_values import (
    new_console_id,
    new_cover_image_url,
    new_definition_id,
    new_game_id,
    new_game_title,
    new_genre_name,
    new_identity_sub,
    new_percent_completed,
    new_share_slug,
)


class FakeCollectionsRepository:
    def __init__(self, definitions=None, items_by_definition=None):
        self._by_slug = {d.share_slug: d for d in (definitions or []) if d.share_slug is not None}
        self._items = items_by_definition or {}

    async def get_definition_by_share_slug(self, share_slug):
        definition = self._by_slug.get(share_slug)
        if definition is None or definition.visibility == VISIBILITY_PRIVATE:
            return None
        return definition

    async def list_definition_items(self, definition_id):
        return self._items.get(definition_id, [])


def _definition(**overrides):
    return replace(
        CollectionDefinition(
            definition_id=new_definition_id(),
            identity_sub=new_identity_sub(),
            name=new_game_title(),
            kind=FILTER_LIST_KIND,
            console_id=None,
            genre_filter=(),
            min_score=None,
            aaa_tier_filter=None,
            sort_order=None,
            description=new_game_title(),
            visibility=VISIBILITY_PUBLIC,
            share_slug=new_share_slug(),
        ),
        **overrides,
    )


def _item():
    return CollectionItem(
        game_id=new_game_id(),
        rank=1,
        title=new_game_title(),
        franchise=None,
        genre=new_genre_name(),
        aaa_tier=new_genre_name(),
        critical_score=95.0,
        oc_score=90.0,
        psn_rating=4.8,
        cover_image_url=new_cover_image_url(),
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


def _get(client, share_slug):
    return client.get(_path(client, public_collections_routes.get_public_collection, share_slug=share_slug))


def test_returns_a_public_collection_with_no_auth_header_at_all():
    definition = _definition(visibility=VISIBILITY_PUBLIC)
    item = _item()
    client = _build(FakeCollectionsRepository([definition], {definition.definition_id: [item]}))

    response = _get(client, definition.share_slug)

    assert response.status_code == 200
    body = PublicCollectionResponse.model_validate(response.json())
    assert body.definition_id == definition.definition_id
    assert body.name == definition.name
    assert body.visibility == VISIBILITY_PUBLIC
    assert [served.game_id for served in body.items] == [item.game_id]


def test_returns_an_unlisted_collection_too():
    definition = _definition(visibility=VISIBILITY_UNLISTED)
    client = _build(FakeCollectionsRepository([definition]))

    response = _get(client, definition.share_slug)

    assert response.status_code == 200
    assert PublicCollectionResponse.model_validate(response.json()).visibility == VISIBILITY_UNLISTED


def test_a_private_collections_slug_404s_exactly_like_an_unknown_one():
    definition = _definition(visibility=VISIBILITY_PRIVATE)
    client = _build(FakeCollectionsRepository([definition]))

    response = _get(client, definition.share_slug)

    assert response.status_code == 404


def test_unknown_slug_404s():
    client = _build()

    response = _get(client, new_share_slug())

    assert response.status_code == 404


def test_response_omits_authoring_fields_an_anonymous_viewer_should_not_see():
    console_id, genre = new_console_id(), new_genre_name()
    definition = _definition(
        console_id=console_id, genre_filter=(genre,), min_percent_completed=new_percent_completed()
    )
    client = _build(FakeCollectionsRepository([definition]))

    response = _get(client, definition.share_slug)

    assert response.status_code == 200
    assert console_id not in response.text
    assert genre not in response.text
    assert set(response.json()) == set(PublicCollectionResponse.model_fields)
