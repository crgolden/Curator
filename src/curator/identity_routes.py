from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.deps import require_bearer, require_preference
from curator.psn.account_client import Account, AccountClient, AccountClientFactory
from curator.psn.errors import PsnAuthError
from curator.token_validation import TokenClaims

router = APIRouter(tags=["identity"])

_NO_LINK_DETAIL = "PSN account not linked."
_AUTH_FAILED_DETAIL = "PSN authentication failed; re-link your account."


class IdentityResponse(BaseModel):
    """The ``GET /identity`` response body."""

    account_id: str
    online_id: str
    region: str | None


@router.get("/identity")
async def get_identity(request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]) -> IdentityResponse:
    """Return the caller's own PSN account identity.

    :raises fastapi.HTTPException: 404, if the caller has no PSN link; 403, if ``harvest_identity`` is not
        enabled for this user; 401, if PSN rejects the stored token.
    """
    await require_preference(request, claims.sub, "harvest_identity")

    identity_client_factory: AccountClientFactory = request.app.state.identity_client_factory
    try:
        client: AccountClient = await identity_client_factory(claims.sub)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=_NO_LINK_DETAIL) from exc

    try:
        account = await client.whoami()
    except PsnAuthError as exc:
        raise HTTPException(status_code=401, detail=_AUTH_FAILED_DETAIL) from exc

    return _response(account)


def _response(account: Account) -> IdentityResponse:
    return IdentityResponse(account_id=account.account_id, online_id=account.online_id, region=account.region)
