"""``GET /catalog/games`` -- paginated, filterable browsing of the shared game catalog."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from curator.catalog.repository import CatalogRepository, GameSummary
from curator.catalog.store_backfill_service import StoreBackfillService
from curator.deps import optional_bearer, require_admin
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/catalog", tags=["catalog"])


class GameSummaryResponse(BaseModel):
    """One game in a catalog browsing page."""

    game_id: str
    canonical_title: str
    franchise: str | None
    genre: str | None
    aaa_tier: str | None
    cover_image_url: str | None = None
    store_product_id: str | None = None
    critical_score: float | None = None
    oc_score: float | None = None
    psn_rating: float | None = None
    percent_completed: int | None = None


class CatalogGamesResponse(BaseModel):
    """The ``GET /catalog/games`` response body."""

    games: list[GameSummaryResponse]
    total: int = 0


class CategoryBackfillResult(BaseModel):
    """What one category's walk achieved, and where to resume it."""

    category_id: str
    next_offset: int
    completed: bool
    pages_read: int
    products_seen: int
    games_created: int
    covers_cached: int
    stopped_reason: str | None


class CatalogBackfillResponse(BaseModel):
    """The ``POST /catalog/backfill`` response body."""

    completed: bool
    games_created: int
    covers_cached: int
    categories: list[CategoryBackfillResult]


class CatalogBackfillRequest(BaseModel):
    """Body for ``POST /catalog/backfill``."""

    category_ids: list[str]
    max_pages_per_category: int | None = 20
    start_offsets: dict[str, int] = {}


@router.get("/games", response_model=CatalogGamesResponse)
async def list_games(
    request: Request,
    q: str | None = Query(default=None),
    franchise: str | None = Query(default=None),
    genre: str | None = Query(default=None),
    aaa_tier: str | None = Query(default=None, alias="aaaTier"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> CatalogGamesResponse:
    """Browse the shared game catalog, optionally filtered by title, franchise, genre, or publisher tier.

    :param q: Optional case-insensitive title substring filter.
    :returns: A page of matching games ordered by canonical title, plus the total matching count.
    """
    repository: CatalogRepository = request.app.state.catalog_repository
    games: list[GameSummary]
    games, total = await repository.list_games(
        search=q, franchise=franchise, genre=genre, aaa_tier=aaa_tier, limit=limit, offset=offset
    )
    return CatalogGamesResponse(
        games=[
            GameSummaryResponse(
                game_id=game.game_id,
                canonical_title=game.canonical_title,
                franchise=game.franchise,
                genre=game.genre,
                aaa_tier=game.aaa_tier,
                cover_image_url=game.cover_image_url,
                store_product_id=game.store_product_id,
                critical_score=game.critical_score,
                oc_score=game.oc_score,
                psn_rating=game.psn_rating,
            )
            for game in games
        ],
        total=total,
    )


@router.get("/games/{game_id}", response_model=GameSummaryResponse)
async def get_game(
    request: Request, game_id: str, claims: TokenClaims | None = Depends(optional_bearer)
) -> GameSummaryResponse:
    """Read one catalogued game, including the caller's own trophy progress when they are signed in.

    :raises fastapi.HTTPException: 404, if no game has that id.
    """
    repository: CatalogRepository = request.app.state.catalog_repository
    game = await repository.get_game(game_id, claims.sub if claims else None)
    if game is None:
        raise HTTPException(status_code=404, detail="No such game.")

    return GameSummaryResponse(
        game_id=game.game_id,
        canonical_title=game.canonical_title,
        franchise=game.franchise,
        genre=game.genre,
        aaa_tier=game.aaa_tier,
        cover_image_url=game.cover_image_url,
        store_product_id=game.store_product_id,
        critical_score=game.critical_score,
        oc_score=game.oc_score,
        psn_rating=game.psn_rating,
        percent_completed=game.percent_completed,
    )


@router.post("/backfill", response_model=CatalogBackfillResponse)
async def backfill_catalog(
    request: Request, body: CatalogBackfillRequest, _claims: TokenClaims = Depends(require_admin)
) -> CatalogBackfillResponse:
    """Seed the shared catalog by walking public PlayStation Store categories. Admin-scoped.

    Bounded by ``max_pages_per_category``; each category reports a ``next_offset`` to resume from.

    :returns: Per-category progress plus run totals.
    :raises fastapi.HTTPException: 503, if the store client isn't configured on this deployment.
    """
    backfill_service: StoreBackfillService | None = request.app.state.store_backfill_service
    if backfill_service is None:
        raise HTTPException(status_code=503, detail="PlayStation Store catalog client is not configured.")

    summary = await backfill_service.backfill(
        body.category_ids,
        max_pages_per_category=body.max_pages_per_category,
        start_offsets=body.start_offsets,
    )
    return CatalogBackfillResponse(
        completed=summary.completed,
        games_created=summary.games_created,
        covers_cached=summary.covers_cached,
        categories=[
            CategoryBackfillResult(
                category_id=progress.category_id,
                next_offset=progress.next_offset,
                completed=progress.completed,
                pages_read=progress.pages_read,
                products_seen=progress.products_seen,
                games_created=progress.games_created,
                covers_cached=progress.covers_cached,
                stopped_reason=progress.stopped_reason,
            )
            for progress in summary.categories
        ],
    )
