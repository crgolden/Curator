"""PSN account link/unlink routes.

Both routes act exclusively on ``require_verified_caller``'s resolved caller -- the request body never
carries (and the routes never accept) a caller-supplied user identifier, so one user can never link or
unlink another's PSN account.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from curator.deps import require_verified_caller
from curator.link_service import AgentFactory, LinkError
from curator.link_service import link as link_account
from curator.link_service import unlink as unlink_account
from curator.me_routes import PsnSummary
from curator.persistence.crypto import TokenCrypto
from curator.persistence.repository import Repository
from curator.reverify import reverify_link
from curator.token_validation import TokenClaims

router = APIRouter(tags=["account"])

_ERROR_STATUS = {
    "invalid_npsso": 400,
    "auth_failed": 401,
    "mismatch": 409,
    "unverified": 409,
}
_ERROR_DETAIL = {
    "mismatch": "emails do not match",
    "unverified": "PSN email is not verified",
    "auth_failed": "PSN authentication failed",
}


class LinkRequest(BaseModel):
    """The ``POST /psn/link`` request body."""

    npsso: str


class LinkResponse(BaseModel):
    """The ``POST /psn/link`` response body."""

    linked: bool
    psn: PsnSummary


@router.post("/psn/link", response_model=LinkResponse)
async def psn_link(
    body: LinkRequest,
    request: Request,
    claims: TokenClaims = Depends(require_verified_caller),
) -> LinkResponse:
    """Link the caller's PSN account, requiring a verified-matching PSN email.

    :raises fastapi.HTTPException: 400 (invalid npsso), 401 (PSN auth failed), or 409 (email mismatch /
        unverified email) -- see :class:`curator.link_service.LinkError`. The body's ``detail`` is
        ``{"error": <LinkError.kind>, "message": <human-readable>}`` so callers can branch on ``error``
        (stable) instead of parsing ``message`` (may change wording).
    """
    repository: Repository = request.app.state.repository
    token_crypto: TokenCrypto = request.app.state.token_crypto
    agent_factory: AgentFactory = request.app.state.agent_factory
    redis_adapter = request.app.state.redis_adapter

    # require_verified_caller guarantees claims.email is set before this dependency chain runs.
    assert claims.email is not None, "psn_link requires a verified caller (claims.email must be set)"

    try:
        result = await link_account(
            claims.sub,
            body.npsso,
            claims.email,
            repository=repository,
            token_crypto=token_crypto,
            agent_factory=agent_factory,
            redis=redis_adapter,
        )
    except LinkError as exc:
        status_code = _ERROR_STATUS.get(exc.kind, 400)
        message = _ERROR_DETAIL.get(exc.kind, str(exc))
        raise HTTPException(status_code=status_code, detail={"error": exc.kind, "message": message}) from exc

    return LinkResponse(
        linked=True,
        psn=PsnSummary(
            access_token_expires_at=_iso(result.access_token_expires_at),
            refresh_token_expires_at=_iso(result.refresh_token_expires_at),
        ),
    )


@router.delete("/psn/link", status_code=204)
async def psn_unlink(
    request: Request,
    claims: TokenClaims = Depends(require_verified_caller),
) -> Response:
    """Re-verify (see :func:`curator.reverify.reverify_link`), then unlink the caller's PSN account."""
    repository: Repository = request.app.state.repository
    token_crypto: TokenCrypto = request.app.state.token_crypto
    agent_factory: AgentFactory = request.app.state.agent_factory
    redis_adapter = request.app.state.redis_adapter

    await reverify_link(
        claims, repository=repository, token_crypto=token_crypto, agent_factory=agent_factory, redis=redis_adapter
    )
    await unlink_account(claims.sub, repository=repository, token_crypto=token_crypto, redis=redis_adapter)
    return Response(status_code=204)


def _iso(value: datetime | None) -> str | None:
    """Render a datetime as ISO-8601, or ``None``."""
    return value.isoformat() if value is not None else None
