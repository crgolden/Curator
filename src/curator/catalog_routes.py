from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from curator.catalog.repository import CatalogRepository, GameSummary
from curator.catalog.store_backfill_service import StoreBackfillService
from curator.deps import optional_bearer, require_admin
from curator.psn.store_client import PRODUCT_GENRES_FACET, PS4_GAMES_CATEGORY_ID, StoreCatalogClient
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


class CatalogGenresResponse(BaseModel):
    """The ``GET /catalog/genres`` response body."""

    genres: list[str] = []


class CatalogGenreDriftResponse(BaseModel):
    """The ``GET /catalog/genres/drift`` response body."""

    category_id: str
    missing_from_table: list[str] = []
    missing_from_facet: list[str] = []
    matched: int = 0


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


@router.get("/games")
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


@router.get("/games/{game_id}")
async def get_game(
    request: Request, game_id: str, claims: Annotated[TokenClaims | None, Depends(optional_bearer)]
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


@router.get("/genres")
async def list_genres(request: Request) -> CatalogGenresResponse:
    """List the genres ``GET /catalog/games`` can actually be filtered by.

    :returns: Every genre assigned to at least one game, ordered by curation priority.
    """
    repository: CatalogRepository = request.app.state.catalog_repository
    return CatalogGenresResponse(genres=await repository.list_genres())


@router.get("/genres/drift")
async def genre_vocabulary_drift(
    request: Request,
    _claims: Annotated[TokenClaims, Depends(require_admin)],
    category_id: str = Query(default=PS4_GAMES_CATEGORY_ID, alias="categoryId"),
) -> CatalogGenreDriftResponse:
    """Report where the ``genres`` reference table and the storefront's live ``productGenres`` facet
    disagree. Admin-scoped.

    **This endpoint writes nothing.** It never inserts, updates or deletes a genre, and it never
    auto-syncs one. Migration ``0036`` seeds the vocabulary from a migration precisely so a rebuilt
    database is byte-identical and independent of PSN being reachable; a reported delta is material for a
    human to turn into the next migration, and applying it here would reintroduce exactly the
    nondeterminism ``0036`` removed. That is a contract, not an implementation detail.

    Comparison is on ``genres.name`` only. ``display_name`` and ``priority`` are Curator's own editorial
    judgment and are deliberately not sourced from PSN, so a difference in either is not facet drift and
    is not reported as one.

    :param category_id: Which storefront category to read the facet from. Must be one that publishes
        ``productGenres``; a category that does not is rejected rather than reported as no drift.
    :returns: Live facet keys with no ``genres`` row, ``genres`` rows the storefront no longer publishes,
        and how many names matched.
    :raises fastapi.HTTPException: 502, if the storefront answered but published no ``productGenres``
        facet for that category. 503 if ``app.state.store_catalog_client`` is absent -- ``create_app``
        always constructs one, so no deployment reaches that arm; it is a guard against a test or a
        future factory that leaves the attribute unset, not a deployment mode.
    """
    store_client: StoreCatalogClient | None = getattr(request.app.state, "store_catalog_client", None)
    if store_client is None:
        raise HTTPException(status_code=503, detail="PlayStation Store catalog client is not configured.")

    census = await store_client.facet_census(category_id, PRODUCT_GENRES_FACET)
    if census is None:
        raise HTTPException(
            status_code=502,
            detail=(
                f"Category {category_id} publishes no '{PRODUCT_GENRES_FACET}' facet, so it cannot answer "
                f"this question. An empty delta here would read as 'no drift' when nothing was compared."
            ),
        )

    repository: CatalogRepository = request.app.state.catalog_repository
    stored = set(await repository.list_genre_vocabulary())
    live = set(census)
    return CatalogGenreDriftResponse(
        category_id=category_id,
        missing_from_table=sorted(live - stored),
        missing_from_facet=sorted(stored - live),
        matched=len(live & stored),
    )


@router.post("/backfill")
async def backfill_catalog(
    request: Request, body: CatalogBackfillRequest, _claims: Annotated[TokenClaims, Depends(require_admin)]
) -> CatalogBackfillResponse:
    """Seed the shared catalog by walking public PlayStation Store categories. Admin-scoped.

    Bounded by ``max_pages_per_category``; each category reports a ``next_offset`` to resume from.

    :returns: Per-category progress plus run totals.
    :raises fastapi.HTTPException: 422, if every requested category yielded no products at all --
        the storefront answers 200 with an empty grid for an id that is not a category, and a 2xx
        would report that as a completed backfill. Its ``detail`` carries the same per-category rows a
        2xx would, so the caller can still see pages read and ``stopped_reason`` per id rather than
        being told only that the whole request failed. 503, if ``app.state.store_backfill_service`` is
        absent -- ``create_app`` always constructs one, so no deployment reaches that arm.
    """
    backfill_service: StoreBackfillService | None = request.app.state.store_backfill_service
    if backfill_service is None:
        raise HTTPException(status_code=503, detail="PlayStation Store backfill service is not configured.")

    summary = await backfill_service.backfill(
        body.category_ids,
        max_pages_per_category=body.max_pages_per_category,
        start_offsets=body.start_offsets,
    )
    categories = [
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
    ]
    if categories and all(result.stopped_reason == "no_products" for result in categories):
        raise HTTPException(
            status_code=422,
            detail={
                "message": (
                    "Every requested category returned an empty grid. The storefront answered, so these "
                    "are most likely not storefront category ids. A 2xx here would be indistinguishable "
                    "from having backfilled every category."
                ),
                "categories": [result.model_dump(mode="json") for result in categories],
            },
        )

    return CatalogBackfillResponse(
        completed=summary.completed,
        games_created=summary.games_created,
        covers_cached=summary.covers_cached,
        categories=categories,
    )
