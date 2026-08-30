from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.collections.repository import CollectionsRepository, MeasuredSize
from curator.deps import require_bearer
from curator.psn.title_platform import ConsolePlatform, console_platform, platform_vocabulary_message
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/games/{game_id}/measured-sizes", tags=["measured-sizes"])


class MeasuredSizeResponse(BaseModel):
    """One ``game_measured_sizes`` row."""

    game_id: str
    platform: str
    size_gb: float
    recorded_by: str | None
    recorded_at: str


class SetMeasuredSizeRequest(BaseModel):
    """The ``PUT /games/{game_id}/measured-sizes/{platform}`` request body."""

    size_gb: float


def _console_platform(value: str) -> ConsolePlatform:
    try:
        return console_platform(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=platform_vocabulary_message()) from exc


def _to_response(measured_size: MeasuredSize) -> MeasuredSizeResponse:
    return MeasuredSizeResponse(
        game_id=measured_size.game_id,
        platform=measured_size.platform,
        size_gb=measured_size.size_gb,
        recorded_by=measured_size.recorded_by,
        recorded_at=measured_size.recorded_at.isoformat(),
    )


@router.get("", response_model=list[MeasuredSizeResponse])
async def list_measured_sizes(
    request: Request, game_id: str, _claims: TokenClaims = Depends(require_bearer)
) -> list[MeasuredSizeResponse]:
    """Every measured size recorded for this game -- at most one row per platform."""
    repository: CollectionsRepository = request.app.state.collections_repository
    sizes = await repository.list_measured_sizes(game_id)
    return [_to_response(size) for size in sizes]


@router.put("/{platform}", response_model=MeasuredSizeResponse)
async def set_measured_size(
    request: Request,
    game_id: str,
    platform: str,
    body: SetMeasuredSizeRequest,
    claims: TokenClaims = Depends(require_bearer),
) -> MeasuredSizeResponse:
    """Record this game's measured install size for ``platform``, superseding any existing value.

    :raises fastapi.HTTPException: 400, if ``platform`` is outside
        :data:`~curator.psn.title_platform.CONSOLE_PLATFORM_IDS`.
    """
    narrowed_platform = _console_platform(platform)

    repository: CollectionsRepository = request.app.state.collections_repository
    measured_size = await repository.upsert_measured_size(game_id, narrowed_platform, body.size_gb, claims.sub)
    return _to_response(measured_size)
