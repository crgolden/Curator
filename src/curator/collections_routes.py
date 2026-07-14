"""``/collections`` -- generate collections on demand, and save/list/re-run named definitions.

``POST /collections/preview`` is the "try before you save" entry point to :mod:`curator.collections` --
the same on-demand pipeline a saved ``collection_definitions`` row uses, run against a spec the caller
supplies directly in the request body, without persisting anything. ``POST /collections`` saves a named
definition; ``GET /collections`` lists a caller's saved definitions; ``POST /collections/{id}/runs``
generates (and persists, via ``collection_runs``/``collection_items``) a run against a saved definition.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.catalog.repository import CatalogRepository
from curator.collections.collection_orchestrator import CollectionOrchestrator
from curator.collections.collection_spec import CollectionSpec
from curator.collections.game_candidate import GameCandidate
from curator.collections.repository import CollectionDefinition, CollectionsRepository
from curator.deps import require_bearer
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/collections", tags=["collections"])


class CollectionSpecRequest(BaseModel):
    """An inline collection spec, as accepted by ``POST /collections/preview``."""

    kind: str
    console_id: str | None = None
    genre_filter: list[str] = []
    min_score: float | None = None
    aaa_tier_filter: str | None = None


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
    )


class SaveDefinitionRequest(BaseModel):
    """The ``POST /collections`` request body: a name plus the spec to save under it."""

    name: str
    kind: str
    console_id: str | None = None
    genre_filter: list[str] = []
    min_score: float | None = None
    aaa_tier_filter: str | None = None


class DefinitionResponse(BaseModel):
    """One saved collection definition, as returned by ``POST /collections``/``GET /collections``."""

    definition_id: str
    name: str
    kind: str
    console_id: str | None
    genre_filter: list[str]
    min_score: float | None
    aaa_tier_filter: str | None


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
    """Save a named, reusable collection definition for the caller.

    :returns: The saved :class:`DefinitionResponse`.
    :raises fastapi.HTTPException: 400, if ``kind`` is invalid.
    """
    if body.kind not in ("capacity_fill", "filter_list"):
        raise HTTPException(status_code=400, detail="kind must be 'capacity_fill' or 'filter_list'.")

    collections_repository: CollectionsRepository = request.app.state.collections_repository
    definition_id = await collections_repository.save_definition(
        claims.sub,
        body.name,
        CollectionSpec(
            kind=body.kind,
            console_id=body.console_id,
            genre_filter=tuple(body.genre_filter),
            min_score=body.min_score,
            aaa_tier_filter=body.aaa_tier_filter,
        ),
    )
    return _definition_to_response(
        CollectionDefinition(
            definition_id=definition_id,
            identity_sub=claims.sub,
            name=body.name,
            kind=body.kind,
            console_id=body.console_id,
            genre_filter=tuple(body.genre_filter),
            min_score=body.min_score,
            aaa_tier_filter=body.aaa_tier_filter,
            sort_order=None,
        )
    )


@router.get("", response_model=list[DefinitionResponse])
async def list_definitions(request: Request, claims: TokenClaims = Depends(require_bearer)) -> list[DefinitionResponse]:
    """List the caller's saved collection definitions.

    :returns: The caller's :class:`DefinitionResponse` list, newest first.
    """
    collections_repository: CollectionsRepository = request.app.state.collections_repository
    definitions = await collections_repository.list_definitions(claims.sub)
    return [_definition_to_response(definition) for definition in definitions]


@router.post("/{definition_id}/runs", response_model=CollectionRunResponse, status_code=201)
async def run_definition(
    request: Request, definition_id: str, claims: TokenClaims = Depends(require_bearer)
) -> CollectionRunResponse:
    """Generate and persist a run against one of the caller's saved definitions.

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
        result = await orchestrator.generate(claims.sub, definition.to_spec(), size_estimates=size_estimates)
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
        kind=definition.kind,
        console_id=definition.console_id,
        genre_filter=list(definition.genre_filter),
        min_score=definition.min_score,
        aaa_tier_filter=definition.aaa_tier_filter,
    )
