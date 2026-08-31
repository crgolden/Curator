"""``POST`` creates the ``job_runs`` row itself and only then sends the queue message
(:meth:`curator.jobs.queue_publisher.QueuePublisher.publish_enrichment_run`). That order is the contract:
a caller may poll ``GET /enrichment/runs/{run_id}`` immediately after the 202 and must not get a 404. This
is deliberately unlike ``publish_scheduled_library_refresh``, which creates no row -- a schedule's run does
not exist until the worker actually starts it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.deps import require_admin
from curator.jobs.queue_publisher import QueuePublisher
from curator.jobs.repository import JobRunsRepository
from curator.jobs.staleness import abandoned_run_reason
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/enrichment", tags=["enrichment"])

CANCELLED_BY_ADMIN = "An administrator cancelled this enrichment run before it finished."


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


@router.post("/runs", status_code=202)
async def start_enrichment_run(
    request: Request, _claims: Annotated[TokenClaims, Depends(require_admin)]
) -> EnrichmentRunResponse:
    """Queue a global catalog re-enrichment job. Admin-scoped.

    :returns: The live in-flight run's id if one exists, otherwise a newly queued run's id -- an
        in-flight run that has gone stale (:data:`~curator.jobs.staleness.STALE_RUN_THRESHOLD`) is
        marked failed and superseded.
    :raises fastapi.HTTPException: 503, if the job queue isn't configured on this deployment.
    """
    job_runs_repository: JobRunsRepository = request.app.state.job_runs_repository
    active_run = await job_runs_repository.find_active_global_run("enrichment")
    if active_run is not None:
        reason = abandoned_run_reason(active_run, datetime.now(timezone.utc), noun="enrichment run")
        if reason is None:
            return EnrichmentRunResponse(run_id=active_run.run_id)
        await job_runs_repository.mark_failed(active_run.run_id, reason)

    queue_publisher: QueuePublisher | None = request.app.state.queue_publisher
    if queue_publisher is None:
        raise HTTPException(status_code=503, detail="Enrichment queue is not configured.")

    run_id = await queue_publisher.publish_enrichment_run()
    return EnrichmentRunResponse(run_id=run_id)


@router.post("/runs/{run_id}/cancel")
async def cancel_enrichment_run(
    request: Request, run_id: str, _claims: Annotated[TokenClaims, Depends(require_admin)]
) -> EnrichmentRunStatusResponse:
    """Stand a still-running enrichment run down, so a fresh one can be queued. Admin-scoped.

    Cancelling is a terminal ``job_runs.status``, not a row deletion -- see
    ``0046_job_runs_cancelled_status.sql``. Deploy order matters: the migration must be applied to the
    target database before any writer sends the value.

    :returns: The run as it now stands.
    :raises fastapi.HTTPException: 404, if ``run_id`` doesn't exist or isn't an enrichment run; 409, if it
        has already finished, failed or been cancelled.
    """
    job_runs_repository: JobRunsRepository = request.app.state.job_runs_repository
    run = await job_runs_repository.get(run_id)
    if run is None or run.kind != "enrichment":
        raise HTTPException(status_code=404, detail="Enrichment run not found.")

    if not await job_runs_repository.cancel(run_id, CANCELLED_BY_ADMIN):
        raise HTTPException(status_code=409, detail="This enrichment run has already finished.")

    cancelled = await job_runs_repository.get(run_id)
    if cancelled is None:
        raise HTTPException(status_code=404, detail="Enrichment run not found.")
    return EnrichmentRunStatusResponse(
        run_id=cancelled.run_id,
        status=cancelled.status,
        error=cancelled.error,
        result_summary=cancelled.result_summary,
    )


@router.get("/runs/latest")
async def get_latest_enrichment_run(
    request: Request, _claims: Annotated[TokenClaims, Depends(require_admin)]
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


@router.get("/runs/{run_id}")
async def get_enrichment_run_status(
    request: Request, run_id: str, _claims: Annotated[TokenClaims, Depends(require_admin)]
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
