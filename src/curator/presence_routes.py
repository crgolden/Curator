"""``GET /presence`` -- the caller's own current PSN online presence.

Gated on the caller's own ``harvest_presence`` preference via ``curator.deps.require_preference`` -- see
that function's docstring and ``curator.trophy_routes``'s module docstring for the shared no-link-is-404 /
PsnAuthError-is-401 pattern every PSN-data route in this app follows. Never cached (see
``curator.psn.presence_client``'s module docstring): presence is live-only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.deps import require_bearer, require_preference
from curator.psn.errors import PsnAuthError
from curator.psn.models import Presence
from curator.psn.presence_client import PresenceClient, PresenceClientFactory
from curator.token_validation import TokenClaims

router = APIRouter(tags=["presence"])

_NO_LINK_DETAIL = "PSN account not linked."
_AUTH_FAILED_DETAIL = "PSN authentication failed; re-link your account."


class PresenceResponse(BaseModel):
    """The ``GET /presence`` response body."""

    online_status: str | None
    platform: str | None
    last_online_date: str | None
    game_title: str | None


@router.get("/presence", response_model=PresenceResponse)
async def get_presence(request: Request, claims: TokenClaims = Depends(require_bearer)) -> PresenceResponse:
    """Return the caller's own current PSN online presence.

    :raises fastapi.HTTPException: 404, if the caller has no PSN link; 403, if ``harvest_presence`` is not
        enabled for this user; 401, if PSN rejects the stored token.
    """
    await require_preference(request, claims.sub, "harvest_presence")

    presence_client_factory: PresenceClientFactory = request.app.state.presence_client_factory
    try:
        client: PresenceClient = await presence_client_factory(claims.sub)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=_NO_LINK_DETAIL) from exc

    try:
        presence = await client.presence()
    except PsnAuthError as exc:
        raise HTTPException(status_code=401, detail=_AUTH_FAILED_DETAIL) from exc

    return _response(presence)


def _response(presence: Presence) -> PresenceResponse:
    return PresenceResponse(
        online_status=presence.online_status,
        platform=presence.platform,
        last_online_date=presence.last_online_date,
        game_title=presence.game_title,
    )
