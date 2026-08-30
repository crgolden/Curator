"""Every other route in Curator sits behind ``require_bearer`` (see ``curator.deps``'s module docstring).
``GET /public/collections/{share_slug}`` is the deliberate, narrow exception WP4 introduces: a collection's
owner can turn on sharing (``PUT /collections/{id}/visibility``, ``curator.collections_routes``) and hand
out a link that works for someone who has never signed in to Curator at all -- the whole point of a share
link.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from curator.collections.repository import CollectionItem, CollectionsRepository

router = APIRouter(prefix="/public/collections", tags=["public-collections"])


class CollectionItemResponse(BaseModel):
    """One member of a shared collection. Same shape as ``curator.collections_routes
    .CollectionItemResponse`` -- duplicated rather than imported (that one is module-internal, prefixed
    with the assumption only ``collections_routes`` itself builds it), matching how this codebase already
    duplicates small per-module helpers (e.g. ``_parse_retry_after`` in both provider enrichment clients)
    rather than reach across modules for something this small.
    """

    game_id: str
    rank: int
    title: str
    franchise: str | None
    genre: str | None
    aaa_tier: str | None
    critical_score: float | None
    oc_score: float | None
    psn_rating: float | None
    cover_image_url: str | None
    owner_has_access: bool


def _to_item_response(item: CollectionItem) -> CollectionItemResponse:
    return CollectionItemResponse(
        game_id=item.game_id,
        rank=item.rank,
        title=item.title,
        franchise=item.franchise,
        genre=item.genre,
        aaa_tier=item.aaa_tier,
        critical_score=item.critical_score,
        oc_score=item.oc_score,
        psn_rating=item.psn_rating,
        cover_image_url=item.cover_image_url,
        owner_has_access=item.owner_has_access,
    )


class PublicCollectionResponse(BaseModel):
    """The ``GET /public/collections/{share_slug}`` response body.

    Deliberately narrower than the owner-facing ``DefinitionDetailResponse``: no ``console_id``,
    ``genre_filter``, or the other authoring-provenance fields -- an anonymous viewer sees the finished
    list, not how it was built, matching how the module docstring frames what a share link is for.
    """

    definition_id: str
    name: str
    description: str | None
    visibility: str
    items: list[CollectionItemResponse]


@router.get("/{share_slug}", response_model=PublicCollectionResponse)
async def get_public_collection(request: Request, share_slug: str) -> PublicCollectionResponse:
    """Return a shared collection by its public link -- no ``Authorization`` header, no caller identity.

    :raises fastapi.HTTPException: 404, if ``share_slug`` is unknown or names a currently-``"private"``
        collection (the two are indistinguishable here by design -- see the module docstring).
    """
    collections_repository: CollectionsRepository = request.app.state.collections_repository
    definition = await collections_repository.get_definition_by_share_slug(share_slug)
    if definition is None:
        raise HTTPException(status_code=404, detail="Collection not found.")

    items = await collections_repository.list_definition_items(definition.definition_id)
    return PublicCollectionResponse(
        definition_id=definition.definition_id,
        name=definition.name,
        description=definition.description,
        visibility=definition.visibility,
        items=[_to_item_response(item) for item in items],
    )
