from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from curator.audit.repository import (
    ACTION_LINK_FAILED,
    ACTION_LINK_SUCCEEDED,
    ACTION_UNLINKED,
    AccountActionLogRepository,
)
from curator.deps import require_verified_caller
from curator.library.repository import LibraryRepository
from curator.link_service import AgentFactory, LinkError
from curator.link_service import link as link_account
from curator.link_service import unlink as unlink_account
from curator.me_routes import PsnSummary
from curator.persistence.crypto import TokenCrypto
from curator.persistence.refresh_schedules_repository import RefreshSchedulesRepository
from curator.persistence.repository import Repository
from curator.reverify import reverify_link
from curator.token_validation import TokenClaims

logger = logging.getLogger("curator")

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
    audit_repository: AccountActionLogRepository = request.app.state.audit_repository

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
        await _log(audit_repository, claims.sub, ACTION_LINK_FAILED, exc.kind)
        status_code = _ERROR_STATUS.get(exc.kind, 400)
        message = _ERROR_DETAIL.get(exc.kind, str(exc))
        raise HTTPException(status_code=status_code, detail={"error": exc.kind, "message": message}) from exc

    await _log(audit_repository, claims.sub, ACTION_LINK_SUCCEEDED)
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
    audit_repository: AccountActionLogRepository = request.app.state.audit_repository
    library_repository: LibraryRepository = request.app.state.library_repository

    await reverify_link(
        claims, repository=repository, token_crypto=token_crypto, agent_factory=agent_factory, redis=redis_adapter
    )
    await unlink_account(claims.sub, repository=repository, token_crypto=token_crypto, redis=redis_adapter)
    await library_repository.clear_trophy_progress(claims.sub)
    refresh_schedules_repository: RefreshSchedulesRepository = request.app.state.refresh_schedules_repository
    await refresh_schedules_repository.delete(claims.sub)
    await _log(audit_repository, claims.sub, ACTION_UNLINKED)
    return Response(status_code=204)


def _iso(value: datetime | None) -> str | None:
    """Render a datetime as ISO-8601, or ``None``."""
    return value.isoformat() if value is not None else None


async def _log(audit_repository: AccountActionLogRepository, sub: str, action: str, detail: str | None = None) -> None:
    """Write one audit entry, never letting a logging failure break the user-facing request.

    :param audit_repository: The :class:`~curator.audit.repository.AccountActionLogRepository`.
    :param sub: The Identity ``sub`` claim of the affected user.
    :param action: One of the ``account_action_log.action`` CHECK values.
    :param detail: A short human-readable summary (never the npsso, a token, or raw PSN data).
    """
    try:
        await audit_repository.log(sub, action, detail)
    except Exception:
        logger.exception("Failed to write account_action_log entry (sub=%s, action=%s)", sub, action)
