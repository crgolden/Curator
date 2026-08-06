"""``GET /games/{game_id}/measured-sizes`` / ``PUT /games/{game_id}/measured-sizes/{platform}`` -- WP13's
global, per-(game, platform) contributed install-size cache.

Distinct from every other route in this file family (``consoles_routes``, ``storage_devices_routes``):
those are all owner-scoped, this one is not. A measured install size is a property of the game/platform
pair, not of any one user's library -- the same "shared catalog fact, not per-user data" reasoning as
``game_enrichment`` (see ``0001_initial.sql``'s own module docstring) -- so ``PUT`` accepts any
authenticated caller, not just the game's "owner" (games have no owner). ``recorded_by`` is captured purely
as an accountability trail; see ``curator.collections.repository.MeasuredSize`` and migration ``0025`` for
why it is nullable rather than a hard ownership link.
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
    """Every measured size recorded for this game -- at most one row per platform (``PS5``/``PS4``), an
    empty list if nobody has contributed one yet."""
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
    """Record (or overwrite) this game's measured install size for ``platform``. Any authenticated user
    may contribute -- see this module's docstring for why that's the settled design, not an oversight.

    :raises fastapi.HTTPException: 400, if ``platform`` isn't ``"PS5"`` or ``"PS4"`` -- checked here,
        matching ``storage_devices_routes.create_storage_device``'s ``kind`` validation, rather than left
        to the table's own ``CHECK`` so a bad value gets a clear message instead of an opaque 500.
    """
    if platform not in _VALID_PLATFORMS:
        raise HTTPException(status_code=400, detail='platform must be "PS5" or "PS4".')

    repository: CollectionsRepository = request.app.state.collections_repository
    measured_size = await repository.upsert_measured_size(game_id, platform, body.size_gb, claims.sub)
    return _to_response(measured_size)
