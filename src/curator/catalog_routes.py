"""``GET /catalog/games`` -- paginated, filterable browsing of the shared game catalog."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel

from curator.catalog.repository import CatalogRepository, GameSummary
from curator.deps import require_bearer
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/catalog", tags=["catalog"])


class GameSummaryResponse(BaseModel):
    """One game in a catalog browsing page."""

    game_id: str
    canonical_title: str
    franchise: str | None
    genre: str | None
    aaa_tier: str | None


class CatalogGamesResponse(BaseModel):
    """The ``GET /catalog/games`` response body."""

    games: list[GameSummaryResponse]


@router.get("/games", response_model=CatalogGamesResponse)
async def list_games(
    request: Request,
    franchise: str | None = Query(default=None),
    genre: str | None = Query(default=None),
    aaa_tier: str | None = Query(default=None, alias="aaaTier"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _claims: TokenClaims = Depends(require_bearer),
) -> CatalogGamesResponse:
    """Browse the shared game catalog, optionally filtered by franchise, genre, or publisher tier.

    :returns: A page of matching games, ordered by canonical title.
    """
    repository: CatalogRepository = request.app.state.catalog_repository
    games: list[GameSummary] = await repository.list_games(
        franchise=franchise, genre=genre, aaa_tier=aaa_tier, limit=limit, offset=offset
    )
    return CatalogGamesResponse(
        games=[
            GameSummaryResponse(
                game_id=game.game_id,
                canonical_title=game.canonical_title,
                franchise=game.franchise,
                genre=game.genre,
                aaa_tier=game.aaa_tier,
            )
            for game in games
        ]
    )
