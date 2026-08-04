"""``POST /enrichment/runs`` -- queues a global catalog re-enrichment job. Admin-scoped.

Publishes to the ``curator-enrichment`` Service Bus queue and returns immediately; the actual work
(:mod:`curator.app`'s ``_enrichment_run_handler``) runs on :mod:`curator.jobs.queue_consumer`'s own
schedule -- this is exactly the kind of bursty, rate-limited backfill the migration plan's rate-limit
section calls for moving to a background worker rather than an inline-blocking request. That handler
runs four passes: an OpenCritic cache refresh, franchise reclassification for every catalog game,
``aaa_tier`` reclassification for every already-enriched game, and best-effort full enrichment
(RAWG + OpenCritic; no PSN-catalog signal, which needs a per-user authenticated session this
admin-only pass doesn't have) for games nobody has enriched yet.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.deps import require_admin
from curator.jobs.queue_publisher import QueuePublisher
from curator.jobs.repository import JobRunsRepository
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/enrichment", tags=["enrichment"])


class EnrichmentRunResponse(BaseModel):
    """The ``POST /enrichment/runs`` response body."""

    run_id: str


class EnrichmentRunStatusResponse(BaseModel):
    """The ``GET /enrichment/runs/latest`` and ``GET /enrichment/runs/{run_id}`` response body -- mirrors
    ``LibraryRefreshStatusResponse``'s shape (``curator.library_routes``)."""

    run_id: str
    status: str
    error: str | None
    result_summary: dict[str, Any] | None


@router.post("/runs", response_model=EnrichmentRunResponse, status_code=202)
async def start_enrichment_run(
    request: Request, _claims: TokenClaims = Depends(require_admin)
) -> EnrichmentRunResponse:
    """Queue a global catalog re-enrichment job. Admin-scoped.

    Refreshes the OpenCritic cache, reclassifies franchise for every catalog game and ``aaa_tier``
    for every already-enriched game against the current curation-rule tables, and best-effort
    enriches (RAWG + OpenCritic) any game nobody has enriched yet.

    :returns: The new job's run id.
    :raises fastapi.HTTPException: 503, if the job queue isn't configured on this deployment.
    """
    queue_publisher: QueuePublisher | None = request.app.state.queue_publisher
    if queue_publisher is None:
        raise HTTPException(status_code=503, detail="Enrichment queue is not configured.")

    run_id = await queue_publisher.publish_enrichment_run()
    return EnrichmentRunResponse(run_id=run_id)


@router.get("/runs/latest", response_model=EnrichmentRunStatusResponse)
async def get_latest_enrichment_run(
    request: Request, _claims: TokenClaims = Depends(require_admin)
) -> EnrichmentRunStatusResponse:
    """Return the most recently queued enrichment run's status. Admin-scoped.

    Declared before ``GET /enrichment/runs/{run_id}`` -- FastAPI matches routes in registration order, so
    a ``run_id={run_id}`` route declared first would capture ``/runs/latest`` as ``run_id="latest"``.

    :returns: The latest run's status.
    :raises fastapi.HTTPException: 404, if no enrichment run has ever been queued.
    """
    job_runs_repository: JobRunsRepository = request.app.state.job_runs_repository
    run = await job_runs_repository.get_latest_by_kind("enrichment")
    if run is None:
        raise HTTPException(status_code=404, detail="No enrichment run has been queued yet.")
    return EnrichmentRunStatusResponse(
        run_id=run.run_id, status=run.status, error=run.error, result_summary=run.result_summary
    )


@router.get("/runs/{run_id}", response_model=EnrichmentRunStatusResponse)
async def get_enrichment_run_status(
    request: Request, run_id: str, _claims: TokenClaims = Depends(require_admin)
) -> EnrichmentRunStatusResponse:
    """Poll one previously queued enrichment run's status. Admin-scoped.

    Unlike ``GET /library/refresh/{run_id}``, not ownership-checked against the caller's own ``sub`` --
    enrichment runs are global (``identity_sub`` is always ``None``), so any admin may poll any run.

    :returns: The run's current status.
    :raises fastapi.HTTPException: 404, if ``run_id`` doesn't exist or isn't an enrichment run.
    """
    job_runs_repository: JobRunsRepository = request.app.state.job_runs_repository
    run = await job_runs_repository.get(run_id)
    if run is None or run.kind != "enrichment":
        raise HTTPException(status_code=404, detail="Enrichment run not found.")
    return EnrichmentRunStatusResponse(
        run_id=run.run_id, status=run.status, error=run.error, result_summary=run.result_summary
    )
