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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.deps import require_admin
from curator.jobs.queue_publisher import QueuePublisher
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/enrichment", tags=["enrichment"])


class EnrichmentRunResponse(BaseModel):
    """The ``POST /enrichment/runs`` response body."""

    run_id: str


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
