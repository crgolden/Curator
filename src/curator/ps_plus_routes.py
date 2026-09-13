"""``GET /me/ps-plus-rotation`` reports the walked PS Plus catalog against the caller's own entitlements:
what they have yet to claim, what is leaving, what entered since the last walk, and which of their
PS-Plus-sourced titles PSN now reports inactive. ``GET /me/ps-plus-rotation/summary`` carries the two
counts with an action attached, for the library page.

Both require a PSN link, because the report subtracts the caller's library and entitlements from the
catalog; without a link there is nothing to subtract. The catalog is the US storefront, the only locale the
walk reads.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.catalog.ps_plus_repository import PsPlusRepository, PsPlusTitle
from curator.deps import require_bearer
from curator.persistence.repository import Repository
from curator.token_validation import TokenClaims

router = APIRouter(tags=["ps-plus"])

_NO_LINK_DETAIL = "PSN account not linked."


class PsPlusTitleResponse(BaseModel):
    """One title in a rotation list.

    :param game_id: The catalog game to link to, or ``None`` when the title is known only to the storefront.
    :param tier: ``extra`` or ``premium``; ``None`` for a lapsed entitlement no walked category lists.
    :param since_at: When the title entered this list.
    """

    title_id: str
    game_id: str | None
    title: str | None
    tier: str | None
    platforms: list[str]
    cover_image_url: str | None
    store_product_id: str | None
    since_at: datetime | None


class PsPlusCategoryResponse(BaseModel):
    """One walked category and its most recent completed walk."""

    tier: str
    walked_at: datetime | None
    total: int


class PsPlusRotationResponse(BaseModel):
    """The ``GET /me/ps-plus-rotation`` response body.

    :param since: The instant ``added`` and ``leaving`` are measured from; ``None`` until every category has
        completed two walks, in which case both lists are empty.
    """

    catalog_walked_at: datetime | None
    since: datetime | None
    added: list[PsPlusTitleResponse]
    leaving: list[PsPlusTitleResponse]
    unclaimed: list[PsPlusTitleResponse]
    lapsed: list[PsPlusTitleResponse]
    categories: list[PsPlusCategoryResponse]


class PsPlusRotationSummaryResponse(BaseModel):
    """The ``GET /me/ps-plus-rotation/summary`` response body."""

    catalog_walked_at: datetime | None
    unclaimed: int
    leaving: int


@router.get("/me/ps-plus-rotation")
async def get_ps_plus_rotation(
    request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> PsPlusRotationResponse:
    """Report the PS Plus Game Catalog and Classics Catalog against the caller's own library.

    :raises fastapi.HTTPException: 404, if the caller has no PSN link.
    """
    await _require_link(request, claims.sub)
    repository: PsPlusRepository = request.app.state.ps_plus_repository
    report = await repository.rotation_report(claims.sub)
    return PsPlusRotationResponse(
        catalog_walked_at=report.catalog_walked_at,
        since=report.since,
        added=[_title(title) for title in report.added],
        leaving=[_title(title) for title in report.leaving],
        unclaimed=[_title(title) for title in report.unclaimed],
        lapsed=[_title(title) for title in report.lapsed],
        categories=[
            PsPlusCategoryResponse(tier=state.tier, walked_at=state.walked_at, total=state.total)
            for state in report.categories
        ],
    )


@router.get("/me/ps-plus-rotation/summary")
async def get_ps_plus_rotation_summary(
    request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> PsPlusRotationSummaryResponse:
    """Count the caller's unclaimed and leaving PS Plus titles.

    :raises fastapi.HTTPException: 404, if the caller has no PSN link.
    """
    await _require_link(request, claims.sub)
    repository: PsPlusRepository = request.app.state.ps_plus_repository
    summary = await repository.rotation_summary(claims.sub)
    return PsPlusRotationSummaryResponse(
        catalog_walked_at=summary.catalog_walked_at, unclaimed=summary.unclaimed, leaving=summary.leaving
    )


async def _require_link(request: Request, sub: str) -> None:
    repository: Repository = request.app.state.repository
    if await repository.get_link(sub) is None:
        raise HTTPException(status_code=404, detail=_NO_LINK_DETAIL)


def _title(title: PsPlusTitle) -> PsPlusTitleResponse:
    return PsPlusTitleResponse(
        title_id=title.title_id,
        game_id=title.game_id,
        title=title.title,
        tier=title.tier,
        platforms=list(title.platforms),
        cover_image_url=title.cover_image_url,
        store_product_id=title.store_product_id,
        since_at=title.since_at,
    )
