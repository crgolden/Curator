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
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.audit.repository import ACTION_LIBRARY_REFRESH_REQUESTED, AccountActionLogRepository
from curator.deps import require_bearer
from curator.jobs.queue_publisher import QueuePublisher
from curator.jobs.repository import JobRunsRepository
from curator.library.repository import LibraryRepository
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/library", tags=["library"])
logger = logging.getLogger("curator")


class LibraryGameResponse(BaseModel):
    """One entry in the ``GET /library`` response."""

    game_id: str
    title: str
    rawg_enriched: bool
    opencritic_enriched: bool


class LibraryRefreshResponse(BaseModel):
    """The ``POST /library/refresh`` response body."""

    run_id: str


class LibraryRefreshStatusResponse(BaseModel):
    """The ``GET /library/refresh/{run_id}`` response body."""

    run_id: str
    status: str
    error: str | None
    result_summary: dict[str, Any] | None


@router.get("", response_model=list[LibraryGameResponse])
async def get_library(request: Request, claims: TokenClaims = Depends(require_bearer)) -> list[LibraryGameResponse]:
    """Return the caller's own library, with per-provider (RAWG/OpenCritic) enrichment status per game.

    Every entry is included, even ones no provider has enriched yet (both flags ``False``) -- this is the
    finished-library view Librarian's ``/library`` page renders as a checkmark table, distinct from
    ``GET /library/refresh/{run_id}``'s job-status polling.
    """
    library_repository: LibraryRepository = request.app.state.library_repository
    games = await library_repository.list_entries_with_enrichment(claims.sub)
    return [
        LibraryGameResponse(
            game_id=game.game_id,
            title=game.title,
            rawg_enriched=game.rawg_enriched,
            opencritic_enriched=game.opencritic_enriched,
        )
        for game in games
    ]


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
