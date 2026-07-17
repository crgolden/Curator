"""``GET``/``PUT /me/psn-preferences`` -- the caller's own PSN data-harvest opt-in flags.

Unlike ``curator.trophy_routes``/``curator.identity_routes``/``curator.presence_routes``/
``curator.devices_routes``, these two routes are not themselves gated by ``curator.deps.require_preference``
-- reading or changing your own preferences is always allowed once you have a PSN link at all; it's the
*other* PSN-data routes that consult the flags this route lets a caller set. Both routes 404 when the
caller has no PSN link (``curator.persistence.repository.Repository.set_psn_preferences`` would otherwise
silently no-op on a write with nothing to update).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.deps import require_bearer
from curator.persistence.repository import LinkRecord, Repository
from curator.token_validation import TokenClaims

router = APIRouter(tags=["preferences"])

_NO_LINK_DETAIL = "PSN account not linked."


class PsnPreferences(BaseModel):
    """The caller's four PSN data-harvest opt-in flags."""

    harvest_trophies: bool
    harvest_identity: bool
    harvest_presence: bool
    harvest_devices: bool


@router.get("/me/psn-preferences", response_model=PsnPreferences)
async def get_psn_preferences(request: Request, claims: TokenClaims = Depends(require_bearer)) -> PsnPreferences:
    """Return the caller's current PSN data-harvest preferences.

    :raises fastapi.HTTPException: 404, if the caller has no PSN link.
    """
    repository: Repository = request.app.state.repository
    link = await repository.get_link(claims.sub)
    if link is None:
        raise HTTPException(status_code=404, detail=_NO_LINK_DETAIL)
    return _response(link)


@router.put("/me/psn-preferences", response_model=PsnPreferences)
async def set_psn_preferences(
    body: PsnPreferences,
    request: Request,
    claims: TokenClaims = Depends(require_bearer),
) -> PsnPreferences:
    """Set the caller's PSN data-harvest preferences (all four flags, in one call).

    :raises fastapi.HTTPException: 404, if the caller has no PSN link.
    """
    repository: Repository = request.app.state.repository
    link = await repository.get_link(claims.sub)
    if link is None:
        raise HTTPException(status_code=404, detail=_NO_LINK_DETAIL)

    await repository.set_psn_preferences(
        claims.sub,
        harvest_trophies=body.harvest_trophies,
        harvest_identity=body.harvest_identity,
        harvest_presence=body.harvest_presence,
        harvest_devices=body.harvest_devices,
    )
    return body


def _response(link: LinkRecord) -> PsnPreferences:
    return PsnPreferences(
        harvest_trophies=link.harvest_trophies,
        harvest_identity=link.harvest_identity,
        harvest_presence=link.harvest_presence,
        harvest_devices=link.harvest_devices,
    )
