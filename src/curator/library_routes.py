"""``GET /library`` (the caller's own library, with per-provider enrichment status) and
``POST/GET /library/refresh`` (queue + poll a library-build job).

``POST /library/refresh`` publishes to the ``curator-library-refresh`` Service Bus queue and returns
immediately; the actual ingest -> canonicalize -> persist -> enrich-delta pipeline
(:class:`curator.library.library_build_orchestrator.LibraryBuildOrchestrator`) runs on
:mod:`curator.jobs.queue_consumer`'s own schedule, since it can involve many uncached RAWG/OpenCritic/PSN
calls bound by those services' own rate limits. ``GET /library/refresh/{run_id}`` polls the resulting
:class:`~curator.jobs.repository.JobRun`'s status.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from curator.audit.repository import ACTION_LIBRARY_REFRESH_REQUESTED, AccountActionLogRepository
from curator.deps import require_bearer
from curator.jobs.queue_publisher import QueuePublisher
from curator.jobs.repository import JobRunsRepository
from curator.library.repository import LibraryRepository, LibrarySortField
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/library", tags=["library"])
logger = logging.getLogger("curator")


class LibraryGameResponse(BaseModel):
    """One entry in the ``GET /library`` response."""

    game_id: str
    title: str
    category: str | None
    rawg_rating: float | None
    opencritic_rating: float | None
    psn_rating: float | None
    psn_product_id: str | None
    rawg_enriched: bool
    opencritic_enriched: bool


class LibraryPageResponse(BaseModel):
    """The ``GET /library`` response body: one page of the caller's library plus the total count of
    every row matching the current search/filter, independent of ``limit``/``offset``."""

    games: list[LibraryGameResponse]
    total: int


class LibraryCategoriesResponse(BaseModel):
    """The ``GET /library/categories`` response body."""

    categories: list[str]


class LibraryRefreshResponse(BaseModel):
    """The ``POST /library/refresh`` response body."""

    run_id: str


class LibraryRefreshStatusResponse(BaseModel):
    """The ``GET /library/refresh/{run_id}`` response body."""

    run_id: str
    status: str
    error: str | None
    result_summary: dict[str, Any] | None


@router.get("/categories", response_model=LibraryCategoriesResponse)
async def get_library_categories(
    request: Request, claims: TokenClaims = Depends(require_bearer)
) -> LibraryCategoriesResponse:
    """Return the distinct, sorted set of categories (resolved genres) present in the caller's own
    library -- backs the library page's category filter dropdown."""
    library_repository: LibraryRepository = request.app.state.library_repository
    categories = await library_repository.list_categories(claims.sub)
    return LibraryCategoriesResponse(categories=categories)


@router.get("", response_model=LibraryPageResponse)
async def get_library(
    request: Request,
    q: str | None = Query(default=None),
    category: str | None = Query(default=None),
    sort: LibrarySortField = Query(default="title"),
    sort_dir: Literal["asc", "desc"] = Query(default="asc", alias="sortDir"),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    claims: TokenClaims = Depends(require_bearer),
) -> LibraryPageResponse:
    """Return one page of the caller's own library, with per-provider (RAWG/OpenCritic) ratings,
    the resolved category, and PSN's own catalog rating/product id per game.

    Every entry is included, even ones no provider has enriched yet (all rating fields ``None``) --
    this is the finished-library view Librarian's ``/library`` page renders, distinct from
    ``GET /library/refresh/{run_id}``'s job-status polling.

    :param q: Optional case-insensitive title substring filter.
    :param category: Optional exact-match category (resolved genre name) filter.
    :param sort: Which column to sort by.
    :param sort_dir: Sort direction; unresolved (``None``) values always sort last regardless.
    :param limit: Page size.
    :param offset: Number of matching rows to skip.
    """
    library_repository: LibraryRepository = request.app.state.library_repository
    games, total = await library_repository.list_entries_with_enrichment(
        claims.sub, search=q, category=category, sort=sort, sort_dir=sort_dir, limit=limit, offset=offset
    )
    return LibraryPageResponse(
        games=[
            LibraryGameResponse(
                game_id=game.game_id,
                title=game.title,
                category=game.category,
                rawg_rating=game.rawg_rating,
                opencritic_rating=game.opencritic_rating,
                psn_rating=game.psn_rating,
                psn_product_id=game.psn_product_id,
                rawg_enriched=game.rawg_enriched,
                opencritic_enriched=game.opencritic_enriched,
            )
            for game in games
        ],
        total=total,
    )


@router.post("/refresh", response_model=LibraryRefreshResponse, status_code=202)
async def refresh_library(request: Request, claims: TokenClaims = Depends(require_bearer)) -> LibraryRefreshResponse:
    """Queue a library-refresh job for the caller's own PSN entitlements.

    :returns: The new job's run id.
    :raises fastapi.HTTPException: 503, if the job queue isn't configured on this deployment.
    """
    queue_publisher: QueuePublisher | None = request.app.state.queue_publisher
    if queue_publisher is None:
        raise HTTPException(status_code=503, detail="Library refresh queue is not configured.")

    run_id = await queue_publisher.publish_library_refresh(claims.sub)
    audit_repository: AccountActionLogRepository = request.app.state.audit_repository
    try:
        await audit_repository.log(claims.sub, ACTION_LIBRARY_REFRESH_REQUESTED, run_id)
    except Exception:
        logger.exception(
            "Failed to write account_action_log entry (sub=%s, action=%s)", claims.sub, ACTION_LIBRARY_REFRESH_REQUESTED
        )
    return LibraryRefreshResponse(run_id=run_id)


@router.get("/refresh/{run_id}", response_model=LibraryRefreshStatusResponse)
async def get_library_refresh_status(
    request: Request, run_id: str, claims: TokenClaims = Depends(require_bearer)
) -> LibraryRefreshStatusResponse:
    """Poll the status of a previously queued library-refresh job.

    :returns: The run's current :class:`LibraryRefreshStatusResponse`.
    :raises fastapi.HTTPException: 404, if ``run_id`` doesn't exist or isn't the caller's own run.
    """
    job_runs_repository: JobRunsRepository = request.app.state.job_runs_repository
    run = await job_runs_repository.get(run_id)
    if run is None or run.kind != "library_refresh" or run.identity_sub != claims.sub:
        raise HTTPException(status_code=404, detail="Library refresh run not found.")

    return LibraryRefreshStatusResponse(
        run_id=run.run_id, status=run.status, error=run.error, result_summary=run.result_summary
    )
