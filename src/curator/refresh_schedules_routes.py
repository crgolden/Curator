"""``GET`` and ``PUT`` require a PSN link -- a schedule exists to spend a stored PSN token, so one without a
link has nothing to act on. ``DELETE`` deliberately does not, and that asymmetry is the point: unlinking
already removes any schedule, so requiring a link to delete one would make an orphaned schedule
undeletable by the only person entitled to remove it. The delete is scoped to the caller's own ``sub`` and
is idempotent, so a caller with no link simply gets a 204 and nothing happens.

Saving a schedule also publishes its first scheduled message; the runtime that consumes it is not this one
(see ``AGENTS/Functions.md`` for the message contract).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from curator.deps import require_bearer
from curator.jobs.queue_publisher import QueuePublisher
from curator.persistence.refresh_schedules_repository import (
    Cadence,
    RefreshSchedule,
    RefreshSchedulesRepository,
    next_run_after,
)
from curator.persistence.repository import Repository
from curator.token_validation import TokenClaims

router = APIRouter(tags=["refresh-schedule"])

_NO_LINK_DETAIL = "PSN account not linked."
_NO_SCHEDULE_DETAIL = "No refresh schedule configured."
_NO_QUEUE_DETAIL = "Scheduled refreshes are not configured."


class RefreshScheduleRequest(BaseModel):
    """A caller's requested schedule.

    :param ps_plus_watch: Whether each run should also report which titles entered or left the PS Plus
        catalog, rather than only refreshing entitlements.
    """

    cadence: Cadence
    ps_plus_watch: bool = False


class RefreshScheduleResponse(BaseModel):
    """A caller's stored schedule and the state of its chain.

    :param paused_reason: Why the chain stopped and what the caller must do, or ``None`` while healthy.
    """

    cadence: Cadence
    ps_plus_watch: bool
    next_run_at: datetime
    last_run_at: datetime | None
    consecutive_failures: int
    paused_reason: str | None


@router.get("/me/refresh-schedule", response_model=RefreshScheduleResponse)
async def get_refresh_schedule(
    request: Request, claims: TokenClaims = Depends(require_bearer)
) -> RefreshScheduleResponse:
    """Return the caller's recurring-refresh schedule.

    :raises fastapi.HTTPException: 404, if the caller has no PSN link or no schedule.
    """
    await _require_link(request, claims.sub)
    repository: RefreshSchedulesRepository = request.app.state.refresh_schedules_repository
    schedule = await repository.get(claims.sub)
    if schedule is None:
        raise HTTPException(status_code=404, detail=_NO_SCHEDULE_DETAIL)
    return _response(schedule)


@router.put("/me/refresh-schedule", response_model=RefreshScheduleResponse)
async def set_refresh_schedule(
    body: RefreshScheduleRequest,
    request: Request,
    claims: TokenClaims = Depends(require_bearer),
) -> RefreshScheduleResponse:
    """Create or replace the caller's schedule and publish its first run.

    :raises fastapi.HTTPException: 404, if the caller has no PSN link; 503, if no scheduled-refresh queue
        is configured, since a stored schedule nothing will ever act on is worse than a refused one.
    """
    await _require_link(request, claims.sub)
    queue_publisher: QueuePublisher | None = request.app.state.queue_publisher
    if queue_publisher is None:
        raise HTTPException(status_code=503, detail=_NO_QUEUE_DETAIL)

    repository: RefreshSchedulesRepository = request.app.state.refresh_schedules_repository
    next_run_at = next_run_after(body.cadence)
    schedule = await repository.upsert(
        claims.sub, cadence=body.cadence, ps_plus_watch=body.ps_plus_watch, next_run_at=next_run_at
    )
    await queue_publisher.publish_scheduled_library_refresh(claims.sub, next_run_at)
    return _response(schedule)


@router.delete("/me/refresh-schedule", status_code=204)
async def delete_refresh_schedule(request: Request, claims: TokenClaims = Depends(require_bearer)) -> Response:
    """Remove the caller's schedule, withdrawing the opt-in.

    Any message already scheduled is left to fire and be discarded by the processor, which finds no row to
    act on -- Service Bus cannot recall a scheduled message this app no longer holds the sequence number
    for, and the absent row is already the authoritative answer.
    """
    repository: RefreshSchedulesRepository = request.app.state.refresh_schedules_repository
    await repository.delete(claims.sub)
    return Response(status_code=204)


async def _require_link(request: Request, sub: str) -> None:
    repository: Repository = request.app.state.repository
    if await repository.get_link(sub) is None:
        raise HTTPException(status_code=404, detail=_NO_LINK_DETAIL)


def _response(schedule: RefreshSchedule) -> RefreshScheduleResponse:
    return RefreshScheduleResponse(
        cadence=schedule.cadence,
        ps_plus_watch=schedule.ps_plus_watch,
        next_run_at=schedule.next_run_at,
        last_run_at=schedule.last_run_at,
        consecutive_failures=schedule.consecutive_failures,
        paused_reason=schedule.paused_reason,
    )
