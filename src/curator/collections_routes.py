"""``/collections`` -- create, read, edit, delete and re-run a user's named collections.

A collection is an **explicit, ordered list of games the owner chose**, stored in
``collection_definition_items``. It is not a live predicate. The owner can edit that list whenever they
like via ``PATCH``; what it never does is change on its own. The filter fields on a definition
(``genre_filter``/``min_score``/``aaa_tier_filter``) are provenance and an authoring aid -- they say how
the list was first assembled and let the owner ask for a fresh proposal -- but membership only ever
changes when the owner asks for it.

That distinction is what makes a collection shareable: a viewer who is not the owner sees exactly the
titles the owner put in it, rather than whatever re-running a filter against somebody's library would
produce.

``POST /collections/preview`` is the "try before you save" entry point to :mod:`curator.collections` --
the same on-demand pipeline, run against a spec supplied inline, persisting nothing. ``POST /collections``
saves a collection; ``GET``/``PATCH``/``DELETE /collections/{id}`` read one (with its items), edit it, and
remove it; ``POST /collections/{id}/runs`` generates and persists a run against the saved spec, and is the
one path that deliberately does *not* touch membership.
"""

from __future__ import annotations

from typing import Any, Literal

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from curator.catalog.repository import CatalogRepository
from curator.collections.collection_orchestrator import CollectionOrchestrator
from curator.collections.collection_spec import CollectionSpec
from curator.collections.filter_predicate import FilterPredicate, parse_predicate, predicate_to_dict
from curator.collections.game_candidate import GameCandidate
from curator.collections.repository import (
    CollectionDefinition,
    CollectionItem,
    CollectionItemSortField,
    CollectionsRepository,
)
from curator.deps import require_bearer
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/collections", tags=["collections"])


def _parse_filter_predicate(raw: dict[str, Any] | None) -> FilterPredicate | None:
    """Parse a request body's ``filter_predicate`` (WP8), or ``None`` if omitted.

    :raises fastapi.HTTPException: 400, if ``raw`` is not a well-formed predicate tree.
    """
    if raw is None:
        return None
    try:
        return parse_predicate(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class CollectionSpecRequest(BaseModel):
    """An inline collection spec, as accepted by ``POST /collections/preview``."""

    kind: str
    console_id: str | None = None
    genre_filter: list[str] = []
    min_score: float | None = None
    aaa_tier_filter: str | None = None
    filter_predicate: dict[str, Any] | None = None
    include_inactive: bool = False
    min_percent_completed: int | None = None
    sort_order: str | None = None
    exclude_installed_on: list[str] = []


class CollectionGameResponse(BaseModel):
    """One game in a collection-preview result."""

    game_id: str
    title: str
    genre: str
    aaa_tier: str
    franchise: str
    composite_score: float | None
    rank_score: int
    size_gb: float
    percent_completed: int | None


class CollectionPreviewResponse(BaseModel):
    """The ``POST /collections/preview`` response body."""

    included: list[CollectionGameResponse]
    excluded: list[CollectionGameResponse]
    used_gb: float | None


@router.post("/preview", response_model=CollectionPreviewResponse)
async def preview_collection(
    request: Request, spec: CollectionSpecRequest, claims: TokenClaims = Depends(require_bearer)
) -> CollectionPreviewResponse:
    """Generate a collection from an inline spec for the caller's own library, without persisting it.

    :returns: The generated :class:`CollectionPreviewResponse`.
    :raises fastapi.HTTPException: 400, if ``kind`` is invalid or (for ``"capacity_fill"``) ``console_id``
        is missing or unknown.
    """
    if spec.kind not in ("capacity_fill", "filter_list"):
        raise HTTPException(status_code=400, detail="kind must be 'capacity_fill' or 'filter_list'.")

    filter_predicate = _parse_filter_predicate(spec.filter_predicate)
    orchestrator: CollectionOrchestrator = request.app.state.collection_orchestrator
    catalog_repository: CatalogRepository = request.app.state.catalog_repository
    size_estimates = await catalog_repository.get_size_estimates()

    try:
        result = await orchestrator.generate(
            claims.sub,
            CollectionSpec(
                kind=spec.kind,
                console_id=spec.console_id,
                genre_filter=tuple(spec.genre_filter),
                min_score=spec.min_score,
                aaa_tier_filter=spec.aaa_tier_filter,
                filter_predicate=filter_predicate,
                include_inactive=spec.include_inactive,
                min_percent_completed=spec.min_percent_completed,
                sort_order=spec.sort_order,
                exclude_installed_on=tuple(spec.exclude_installed_on),
            ),
            size_estimates=size_estimates,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return CollectionPreviewResponse(
        included=[_to_response(candidate) for candidate in result.included],
        excluded=[_to_response(candidate) for candidate in result.excluded],
        used_gb=result.used_gb,
    )


def _to_response(candidate: GameCandidate) -> CollectionGameResponse:
    return CollectionGameResponse(
        game_id=candidate.game_id,
        title=candidate.title,
        genre=candidate.genre,
        aaa_tier=candidate.aaa_tier,
        franchise=candidate.franchise,
        composite_score=candidate.composite_score,
        rank_score=candidate.rank_score,
        size_gb=candidate.size_gb,
        percent_completed=candidate.percent_completed,
    )


class SaveDefinitionRequest(BaseModel):
    """The ``POST /collections`` request body: a name, the games to put in it, and its provenance.

    ``game_ids`` is the collection's membership. ``kind`` and the filter fields describe how the caller
    arrived at that list and default to a plain hand-assembled ``filter_list`` -- a user who picked titles
    by browsing needs to supply neither.
    """

    name: str
    kind: str = "filter_list"
    description: str | None = None
    game_ids: list[str] = []
    console_id: str | None = None
    genre_filter: list[str] = []
    min_score: float | None = None
    aaa_tier_filter: str | None = None
    filter_predicate: dict[str, Any] | None = None
    include_inactive: bool = False
    min_percent_completed: int | None = None
    sort_order: str | None = None
    exclude_installed_on: list[str] = []


class UpdateDefinitionRequest(BaseModel):
    """The ``PATCH /collections/{id}`` request body. Omitted fields are left as they are."""

    name: str | None = None
    description: str | None = None
    game_ids: list[str] | None = None


class DefinitionResponse(BaseModel):
    """One saved collection definition, as returned by ``POST /collections``/``GET /collections``."""

    definition_id: str
    name: str
    description: str | None
    kind: str
    console_id: str | None
    genre_filter: list[str]
    min_score: float | None
    aaa_tier_filter: str | None
    filter_predicate: dict[str, Any] | None
    include_inactive: bool
    min_percent_completed: int | None
    sort_order: str | None
    exclude_installed_on: list[str]
    visibility: str
    share_slug: str | None
    item_count: int


class VisibilityUpdateRequest(BaseModel):
    """The ``PUT /collections/{id}/visibility`` request body."""

    visibility: str


class CollectionItemResponse(BaseModel):
    """One member of a collection, as returned by ``GET /collections/{id}``."""

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


class DefinitionDetailResponse(DefinitionResponse):
    """One collection *and its contents* -- the ``GET /collections/{id}`` response body."""

    items: list[CollectionItemResponse]


DETAIL_PAGE_SIZE = 50


class CollectionItemsPageResponse(BaseModel):
    """One page of a collection's membership -- the ``GET /collections/{id}/items`` response body.

    ``total`` counts every item matching the filters, not the page, so a caller can size its pager without
    walking the whole collection.
    """

    items: list[CollectionItemResponse]
    total: int


class CollectionRunResponse(BaseModel):
    """The ``POST /collections/{definition_id}/runs`` response body: the persisted run plus its results."""

    run_id: str
    included: list[CollectionGameResponse]
    excluded: list[CollectionGameResponse]
    used_gb: float | None


@router.post("", response_model=DefinitionResponse, status_code=201)
async def save_definition(
    request: Request, body: SaveDefinitionRequest, claims: TokenClaims = Depends(require_bearer)
) -> DefinitionResponse:
    """Save a named collection for the caller, freezing ``game_ids`` as its membership.

    ``console_id`` is validated against the caller's own consoles here rather than only at run time --
    otherwise a definition naming someone else's (or a nonexistent) console saves cleanly and fails much
    later, on the first run, with a confusing 400.

    :returns: The saved :class:`DefinitionResponse`.
    :raises fastapi.HTTPException: 400, if ``kind`` is invalid, ``console_id`` is not the caller's, or a
        game id is unknown; 409, if the caller already has a collection with this name.
    """
    if body.kind not in ("capacity_fill", "filter_list"):
        raise HTTPException(status_code=400, detail="kind must be 'capacity_fill' or 'filter_list'.")

    filter_predicate = _parse_filter_predicate(body.filter_predicate)
    collections_repository: CollectionsRepository = request.app.state.collections_repository

    if body.console_id is not None or body.exclude_installed_on:
        consoles = await collections_repository.list_user_consoles(claims.sub)
        owned_console_ids = {console.console_id for console in consoles}
        if body.console_id is not None and body.console_id not in owned_console_ids:
            raise HTTPException(status_code=400, detail=f"Unknown console_id {body.console_id!r} for this user.")
        unknown_exclusions = [cid for cid in body.exclude_installed_on if cid not in owned_console_ids]
        if unknown_exclusions:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown console_id(s) for this user in exclude_installed_on: {unknown_exclusions!r}.",
            )

    game_ids = _normalize_game_ids(body.game_ids)
    await _reject_unknown_games(collections_repository, game_ids)

    try:
        definition_id = await collections_repository.save_definition(
            claims.sub,
            body.name,
            CollectionSpec(
                kind=body.kind,
                console_id=body.console_id,
                genre_filter=tuple(body.genre_filter),
                min_score=body.min_score,
                aaa_tier_filter=body.aaa_tier_filter,
                filter_predicate=filter_predicate,
                include_inactive=body.include_inactive,
                min_percent_completed=body.min_percent_completed,
                sort_order=body.sort_order,
                exclude_installed_on=tuple(body.exclude_installed_on),
            ),
            description=body.description,
            game_ids=game_ids,
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail=f"You already have a collection named {body.name!r}.") from exc

    saved = await collections_repository.get_definition(claims.sub, definition_id)
    assert saved is not None
    return _definition_to_response(saved)


def _normalize_game_ids(game_ids: list[str]) -> tuple[str, ...]:
    """Lower-case every id so client casing can't defeat validation or de-duplication.

    Postgres stores and returns UUIDs in canonical lower case. An uppercase-but-valid id would therefore
    match in SQL, come back lower-cased, and then fail the membership check in
    :func:`_reject_unknown_games` -- a 400 on a game the caller genuinely owns. Worse, ``"550E..."`` and
    ``"550e..."`` survive de-duplication as two distinct strings and then collide on
    ``collection_definition_items``' primary key, which is the exact 500 de-duplication exists to prevent.
    """
    return tuple(game_id.strip().lower() for game_id in game_ids)


async def _reject_unknown_games(collections_repository: CollectionsRepository, game_ids: tuple[str, ...]) -> None:
    """Verify every id names a real game before it can be stored as a collection member.

    Catches the malformed-UUID case explicitly: without it, ``game_ids: ["not-a-uuid"]`` reaches Postgres
    and comes back as a 500 rather than the 400 it plainly is.

    :raises fastapi.HTTPException: 400, if an id is not a UUID or names no game in the shared catalog.
    """
    if not game_ids:
        return
    try:
        known = await collections_repository.existing_game_ids(game_ids)
    except psycopg.errors.InvalidTextRepresentation as exc:
        raise HTTPException(status_code=400, detail="game_ids must be UUIDs.") from exc

    unknown = [game_id for game_id in game_ids if game_id not in known]
    if unknown:
        raise HTTPException(status_code=400, detail=f"Unknown game_ids: {', '.join(sorted(set(unknown)))}.")


@router.get("", response_model=list[DefinitionResponse])
async def list_definitions(request: Request, claims: TokenClaims = Depends(require_bearer)) -> list[DefinitionResponse]:
    """List the caller's saved collection definitions.

    :returns: The caller's :class:`DefinitionResponse` list, newest first.
    """
    collections_repository: CollectionsRepository = request.app.state.collections_repository
    definitions = await collections_repository.list_definitions(claims.sub)
    return [_definition_to_response(definition) for definition in definitions]


@router.get("/followed", response_model=list[DefinitionResponse])
async def list_followed_collections(
    request: Request, claims: TokenClaims = Depends(require_bearer)
) -> list[DefinitionResponse]:
    """List every collection the caller follows, most recently followed first.

    Registered here, before ``@router.get("/{definition_id}")`` below, deliberately -- FastAPI/Starlette
    matches routes in registration order, and ``{definition_id}`` would otherwise swallow a literal
    request for ``GET /collections/followed`` (matching ``definition_id="followed"``) before this route
    ever got a chance to run.
    """
    collections_repository: CollectionsRepository = request.app.state.collections_repository
    definitions = await collections_repository.list_followed_collections(claims.sub)
    return [_definition_to_response(definition) for definition in definitions]


@router.get("/{definition_id}", response_model=DefinitionDetailResponse)
async def get_definition(
    request: Request, definition_id: str, claims: TokenClaims = Depends(require_bearer)
) -> DefinitionDetailResponse:
    """Return one of the caller's collections together with the *first page* of its stored membership.

    ``item_count`` carries the real total; ``GET /collections/{definition_id}/items`` serves every page.

    :returns: The :class:`DefinitionDetailResponse`, items in rank order.
    :raises fastapi.HTTPException: 404, if the collection doesn't exist or isn't the caller's own.
    """
    collections_repository: CollectionsRepository = request.app.state.collections_repository
    definition = await collections_repository.get_definition(claims.sub, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Collection definition not found.")

    items, _ = await collections_repository.list_definition_items_page(definition_id, limit=DETAIL_PAGE_SIZE)
    return DefinitionDetailResponse(
        **_definition_to_response(definition).model_dump(),
        items=[_to_item_response(item) for item in items],
    )


@router.get("/{definition_id}/items", response_model=CollectionItemsPageResponse)
async def get_definition_items(
    request: Request,
    definition_id: str,
    q: str | None = Query(default=None),
    genre: str | None = Query(default=None),
    sort: CollectionItemSortField = Query(default="rank"),
    sort_dir: Literal["asc", "desc"] = Query(default="asc", alias="sortDir"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    claims: TokenClaims = Depends(require_bearer),
) -> CollectionItemsPageResponse:
    """Return one filtered, sorted page of a collection's membership.

    Sorting by anything other than ``rank`` reorders a bin-packed set; it does not repack it.

    :param q: Optional case-insensitive title substring filter.
    :param genre: Optional exact-match resolved-genre filter.
    :param sort: Which column to sort by; unresolved (``None``) values sort last in both directions.
    :param sort_dir: Sort direction.
    :param limit: Page size.
    :param offset: Number of matching items to skip.
    :raises fastapi.HTTPException: 404, if the collection doesn't exist or isn't the caller's own.
    """
    collections_repository: CollectionsRepository = request.app.state.collections_repository
    definition = await collections_repository.get_definition(claims.sub, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Collection definition not found.")

    items, total = await collections_repository.list_definition_items_page(
        definition_id, search=q, genre=genre, sort=sort, sort_dir=sort_dir, limit=limit, offset=offset
    )
    return CollectionItemsPageResponse(items=[_to_item_response(item) for item in items], total=total)


@router.delete("/{definition_id}/items/{game_id}", status_code=204)
async def remove_definition_item(
    request: Request, definition_id: str, game_id: str, claims: TokenClaims = Depends(require_bearer)
) -> Response:
    """Remove one title from a collection, leaving the rest of its membership untouched.

    :raises fastapi.HTTPException: 404, if the collection doesn't exist, isn't the caller's own, or doesn't
        contain that game.
    """
    collections_repository: CollectionsRepository = request.app.state.collections_repository
    definition = await collections_repository.get_definition(claims.sub, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Collection definition not found.")

    if not await collections_repository.remove_definition_item(definition_id, game_id):
        raise HTTPException(status_code=404, detail="That title is not in this collection.")
    return Response(status_code=204)


@router.patch("/{definition_id}", response_model=DefinitionDetailResponse)
async def update_definition(
    request: Request, definition_id: str, body: UpdateDefinitionRequest, claims: TokenClaims = Depends(require_bearer)
) -> DefinitionDetailResponse:
    """Rename a collection, change its description, and/or replace its membership.

    Which fields were actually supplied is read from ``model_fields_set`` rather than by testing for
    ``None`` -- otherwise there would be no way to clear a description, since an explicit ``null`` and an
    omitted field would look identical.

    :returns: The updated :class:`DefinitionDetailResponse`.
    :raises fastapi.HTTPException: 404, if the collection doesn't exist or isn't the caller's own; 400, if
        a game id is unknown; 409, if the new name collides with another of the caller's collections.
    """
    collections_repository: CollectionsRepository = request.app.state.collections_repository
    definition = await collections_repository.get_definition(claims.sub, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Collection definition not found.")

    supplied = body.model_fields_set
    name = body.name if "name" in supplied and body.name is not None else definition.name
    description = body.description if "description" in supplied else definition.description

    game_ids = None if body.game_ids is None else _normalize_game_ids(body.game_ids)
    if game_ids is not None:
        await _reject_unknown_games(collections_repository, game_ids)

    try:
        await collections_repository.update_definition(
            definition_id, name=name, description=description, game_ids=game_ids
        )
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(status_code=409, detail=f"You already have a collection named {name!r}.") from exc

    updated_definition = await collections_repository.get_definition(claims.sub, definition_id)
    assert updated_definition is not None
    items = await collections_repository.list_definition_items(definition_id)
    return DefinitionDetailResponse(
        **_definition_to_response(updated_definition).model_dump(), items=[_to_item_response(item) for item in items]
    )


@router.put("/{definition_id}/visibility", response_model=DefinitionResponse)
async def set_visibility(
    request: Request,
    definition_id: str,
    body: VisibilityUpdateRequest,
    claims: TokenClaims = Depends(require_bearer),
) -> DefinitionResponse:
    """Change a collection's visibility. ``share_slug`` (already assigned at creation regardless of
    visibility -- see migration 0019) starts working as a public link the moment this leaves
    ``"private"``, and stops the moment it's set back.

    :raises fastapi.HTTPException: 404, if the collection doesn't exist or isn't the caller's own; 400, if
        ``visibility`` isn't ``"private"``/``"unlisted"``/``"public"``.
    """
    if body.visibility not in ("private", "unlisted", "public"):
        raise HTTPException(status_code=400, detail='visibility must be "private", "unlisted", or "public".')

    collections_repository: CollectionsRepository = request.app.state.collections_repository
    updated = await collections_repository.set_definition_visibility(claims.sub, definition_id, body.visibility)
    if updated is None:
        raise HTTPException(status_code=404, detail="Collection definition not found.")
    return _definition_to_response(updated)


@router.delete("/{definition_id}", status_code=204)
async def delete_definition(
    request: Request, definition_id: str, claims: TokenClaims = Depends(require_bearer)
) -> Response:
    """Delete one of the caller's collections, along with its items and run history.

    :returns: 204.
    :raises fastapi.HTTPException: 404, if the collection doesn't exist or isn't the caller's own.
    """
    collections_repository: CollectionsRepository = request.app.state.collections_repository
    if not await collections_repository.delete_definition(claims.sub, definition_id):
        raise HTTPException(status_code=404, detail="Collection definition not found.")
    return Response(status_code=204)


@router.post("/{definition_id}/follow", status_code=204)
async def follow_definition(
    request: Request, definition_id: str, claims: TokenClaims = Depends(require_bearer)
) -> Response:
    """Follow a collection that isn't the caller's own.

    :raises fastapi.HTTPException: 404, if the collection doesn't exist or is currently ``"private"`` --
        the two are indistinguishable here, matching :meth:`~curator.collections.repository
        .CollectionsRepository.get_definition_by_share_slug`'s reasoning. 400, if the caller owns it (a
        collection you own is never something you follow).
    """
    collections_repository: CollectionsRepository = request.app.state.collections_repository
    definition = await collections_repository.get_definition_any_owner(definition_id)
    if definition is None or definition.visibility == "private":
        raise HTTPException(status_code=404, detail="Collection definition not found.")
    if definition.identity_sub == claims.sub:
        raise HTTPException(status_code=400, detail="Cannot follow your own collection.")

    await collections_repository.follow_collection(claims.sub, definition_id)
    return Response(status_code=204)


@router.delete("/{definition_id}/follow", status_code=204)
async def unfollow_definition(
    request: Request, definition_id: str, claims: TokenClaims = Depends(require_bearer)
) -> Response:
    """Unfollow a collection. Idempotent -- always 204, even if it wasn't followed, doesn't exist, or has
    since been made private (unfollowing must always be possible, unlike following)."""
    collections_repository: CollectionsRepository = request.app.state.collections_repository
    await collections_repository.unfollow_collection(claims.sub, definition_id)
    return Response(status_code=204)


@router.post("/{definition_id}/runs", response_model=CollectionRunResponse, status_code=201)
async def run_definition(
    request: Request, definition_id: str, claims: TokenClaims = Depends(require_bearer)
) -> CollectionRunResponse:
    """Generate and persist a run against one of the caller's saved definitions.

    This is a *proposal*, not an edit: it re-evaluates the saved spec against the caller's current library
    and records the outcome as run history, leaving the collection's stored membership untouched. Adopting
    a proposal is an explicit ``PATCH /collections/{id}`` with the game ids the caller wants to keep.

    :returns: The persisted :class:`CollectionRunResponse`.
    :raises fastapi.HTTPException: 404, if ``definition_id`` doesn't exist or isn't the caller's own; 400,
        if the saved definition is a ``capacity_fill`` whose console no longer exists.
    """
    collections_repository: CollectionsRepository = request.app.state.collections_repository
    definition = await collections_repository.get_definition(claims.sub, definition_id)
    if definition is None:
        raise HTTPException(status_code=404, detail="Collection definition not found.")

    orchestrator: CollectionOrchestrator = request.app.state.collection_orchestrator
    catalog_repository: CatalogRepository = request.app.state.catalog_repository
    size_estimates = await catalog_repository.get_size_estimates()

    try:
        result = await orchestrator.generate(
            claims.sub,
            definition.to_spec(),
            size_estimates=size_estimates,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    run_id = await collections_repository.save_run(
        claims.sub,
        definition_id,
        {
            "kind": definition.kind,
            "console_id": definition.console_id,
            "genre_filter": list(definition.genre_filter),
            "min_score": definition.min_score,
            "aaa_tier_filter": definition.aaa_tier_filter,
            "filter_predicate": predicate_to_dict(definition.filter_predicate)
            if definition.filter_predicate is not None
            else None,
            "include_inactive": definition.include_inactive,
            "min_percent_completed": definition.min_percent_completed,
            "sort_order": definition.sort_order,
            "exclude_installed_on": list(definition.exclude_installed_on),
        },
        list(result.included),
        list(result.excluded),
    )

    return CollectionRunResponse(
        run_id=run_id,
        included=[_to_response(candidate) for candidate in result.included],
        excluded=[_to_response(candidate) for candidate in result.excluded],
        used_gb=result.used_gb,
    )


def _definition_to_response(definition: CollectionDefinition) -> DefinitionResponse:
    return DefinitionResponse(
        definition_id=definition.definition_id,
        name=definition.name,
        description=definition.description,
        kind=definition.kind,
        console_id=definition.console_id,
        genre_filter=list(definition.genre_filter),
        min_score=definition.min_score,
        aaa_tier_filter=definition.aaa_tier_filter,
        filter_predicate=predicate_to_dict(definition.filter_predicate)
        if definition.filter_predicate is not None
        else None,
        include_inactive=definition.include_inactive,
        min_percent_completed=definition.min_percent_completed,
        sort_order=definition.sort_order,
        exclude_installed_on=list(definition.exclude_installed_on),
        visibility=definition.visibility,
        share_slug=definition.share_slug,
        item_count=definition.item_count,
    )


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
