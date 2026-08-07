"""``GET /games/{game_id}/measured-sizes`` and ``PUT /games/{game_id}/measured-sizes/{platform}`` -- the
global, per-(game, platform) contributed install-size cache. Writable by any authenticated caller.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.collections.repository import CollectionsRepository, MeasuredSize
from curator.deps import require_bearer
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/games/{game_id}/measured-sizes", tags=["measured-sizes"])

_VALID_PLATFORMS = ("PS5", "PS4")


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

    :raises fastapi.HTTPException: 400, if ``platform`` isn't ``"PS5"`` or ``"PS4"``.
    """
    if platform not in _VALID_PLATFORMS:
        raise HTTPException(status_code=400, detail='platform must be "PS5" or "PS4".')

    repository: CollectionsRepository = request.app.state.collections_repository
    measured_size = await repository.upsert_measured_size(game_id, platform, body.size_gb, claims.sub)
    return _to_response(measured_size)
