from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from curator.audit.recorded import ActionRecorder, recorded
from curator.audit.repository import ACTION_LINK_REQUESTED, ACTION_UNLINKED
from curator.deps import require_verified_caller
from curator.library.repository import LibraryRepository
from curator.link_service import (
    LINK_ERROR_AUTH_FAILED,
    LINK_ERROR_INVALID_NPSSO,
    LINK_ERROR_MISMATCH,
    LINK_ERROR_UNVERIFIED,
    AgentFactory,
    LinkError,
)
from curator.link_service import link as link_account
from curator.link_service import unlink as unlink_account
from curator.me_routes import PsnSummary
from curator.persistence.crypto import TokenCrypto
from curator.persistence.refresh_schedules_repository import RefreshSchedulesRepository
from curator.persistence.repository import Repository
from curator.reverify import reverify_link
from curator.token_validation import TokenClaims

router = APIRouter(tags=["account"])

_ERROR_STATUS = {
    LINK_ERROR_INVALID_NPSSO: 400,
    LINK_ERROR_AUTH_FAILED: 401,
    LINK_ERROR_MISMATCH: 409,
    LINK_ERROR_UNVERIFIED: 409,
}
LINK_ERROR_MESSAGES = {
    LINK_ERROR_MISMATCH: "emails do not match",
    LINK_ERROR_UNVERIFIED: "PSN email is not verified",
    LINK_ERROR_AUTH_FAILED: "PSN authentication failed",
}


class LinkErrorDetail(BaseModel):
    """The ``detail`` of a refused ``POST /psn/link``: ``error`` is stable, ``message`` may change wording."""

    error: str
    message: str


class LinkRequest(BaseModel):
    """The ``POST /psn/link`` request body."""

    npsso: str


class LinkResponse(BaseModel):
    """The ``POST /psn/link`` response body."""

    linked: bool
    psn: PsnSummary


@router.post("/psn/link")
async def psn_link(
    body: LinkRequest,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_verified_caller)],
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
    recorder: ActionRecorder = request.app.state.audit_repository

    assert claims.email is not None, "psn_link requires a verified caller (claims.email must be set)"

    async with recorded(recorder, claims.sub, ACTION_LINK_REQUESTED) as entry:
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
            entry.detail = exc.kind
            status_code = _ERROR_STATUS.get(exc.kind, 400)
            message = LINK_ERROR_MESSAGES.get(exc.kind, str(exc))
            detail = LinkErrorDetail(error=exc.kind, message=message).model_dump()
            raise HTTPException(status_code=status_code, detail=detail) from exc

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
    claims: Annotated[TokenClaims, Depends(require_verified_caller)],
) -> Response:
    """Re-verify (see :func:`curator.reverify.reverify_link`), then unlink the caller's PSN account.

    Unlinking also clears any stored trophy progress. That data is PSN-derived and governed by
    ``psn_links.harvest_trophies`` -- but unlinking deletes the ``psn_links`` row, taking the preference
    that governs it with it, so anything left on ``library_entries`` would outlive every control over it
    and keep being served by ``GET /library`` with no way to refresh or verify it. Turning the weaker
    ``harvest_trophies`` toggle off already erases it (see :mod:`curator.preferences_routes`); it would be
    incoherent for the stronger action to preserve it.

    The rest of the library deliberately survives: entitlements and the curated catalog are the user's own
    collection, not PSN telemetry, and unlinking is not account deletion (that is ``DELETE /me``).
    """
    repository: Repository = request.app.state.repository
    token_crypto: TokenCrypto = request.app.state.token_crypto
    agent_factory: AgentFactory = request.app.state.agent_factory
    redis_adapter = request.app.state.redis_adapter
    recorder: ActionRecorder = request.app.state.audit_repository
    library_repository: LibraryRepository = request.app.state.library_repository
    refresh_schedules_repository: RefreshSchedulesRepository = request.app.state.refresh_schedules_repository

    await reverify_link(
        claims,
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=agent_factory,
        recorder=recorder,
        redis=redis_adapter,
    )
    async with recorded(recorder, claims.sub, ACTION_UNLINKED):
        await unlink_account(claims.sub, repository=repository, token_crypto=token_crypto, redis=redis_adapter)
        await library_repository.clear_trophy_progress(claims.sub)
        await refresh_schedules_repository.delete(claims.sub)
    return Response(status_code=204)


def _iso(value: datetime | None) -> str | None:
    """Render a datetime as ISO-8601, or ``None``."""
    return value.isoformat() if value is not None else None
