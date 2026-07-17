"""``GET /me`` -- the caller's identity plus their PSN link status.

Every call re-verifies any existing PSN link against the presented token (see
:func:`curator.reverify.reverify_link`) rather than trusting whatever the link's last-known state was:
identities can change their email, PSN accounts can be re-linked to a different email elsewhere, and a
stale match should not silently keep working forever. The re-verify is itself cheap when there's nothing
new to check -- it only re-hits PSN when the presented token was issued after the link's last
verification.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from curator.audit.repository import ACTION_ACCOUNT_DELETED, AccountActionLogEntry, AccountActionLogRepository
from curator.deps import require_verified_caller
from curator.link_service import AgentFactory
from curator.persistence.crypto import TokenCrypto
from curator.persistence.db_token_store import access_token_cache_key
from curator.persistence.repository import LinkRecord, Repository
from curator.reverify import reverify_link
from curator.token_validation import TokenClaims

router = APIRouter(tags=["account"])


class PsnSummary(BaseModel):
    """A linked PSN account's token expirations, as returned by ``/me`` and ``/psn/link``."""

    access_token_expires_at: str | None
    refresh_token_expires_at: str | None


class MeResponse(BaseModel):
    """The ``GET /me`` response body."""

    sub: str
    email: str | None
    linked: bool
    psn: PsnSummary | None


class AccountActionResponse(BaseModel):
    """One ``account_action_log`` row, as returned by ``GET /me/actions``."""

    action: str
    detail: str | None
    occurred_at: str


class AccountActionsResponse(BaseModel):
    """The ``GET /me/actions`` response body."""

    actions: list[AccountActionResponse]


@router.get("/me", response_model=MeResponse)
async def me(request: Request, claims: TokenClaims = Depends(require_verified_caller)) -> MeResponse:
    """Return the caller's identity plus their PSN link status.

    :returns: ``{"sub", "email", "linked", "psn"}`` where ``psn`` is ``None`` when unlinked, else
        ``{"access_token_expires_at", "refresh_token_expires_at"}`` (ISO-8601 strings, or ``None``).
    """
    repository: Repository = request.app.state.repository
    token_crypto: TokenCrypto = request.app.state.token_crypto
    agent_factory: AgentFactory = request.app.state.agent_factory
    redis_adapter = request.app.state.redis_adapter

    await reverify_link(
        claims, repository=repository, token_crypto=token_crypto, agent_factory=agent_factory, redis=redis_adapter
    )

    link = await repository.get_link(claims.sub)
    return MeResponse(
        sub=claims.sub,
        email=claims.email,
        linked=link is not None,
        psn=_psn_summary(link) if link is not None else None,
    )


@router.delete("/me", status_code=204)
async def delete_me(request: Request, claims: TokenClaims = Depends(require_verified_caller)) -> Response:
    """Delete the caller's account and every trace of data Curator has stored about them.

    Removes the ``app_users`` row for the caller's ``sub``; the ``ON DELETE CASCADE``/FK relationships in
    ``db/migrations/0001_initial.sql`` take care of the PSN link (encrypted tokens), entitlement
    pulls/snapshots, derived library entries/exclusions, consoles, measured sizes, and collections. Cached
    trophy reads (Redis, 15-minute TTL -- see :mod:`curator.psn.trophy_cache`) are keyed by PSN online
    id/account id rather than ``sub`` and self-expire quickly, so they are not explicitly cleared here. The
    cached *access token* (:mod:`curator.persistence.db_token_store`), however, is a live bearer credential
    rather than read-only cache data, so it is deleted explicitly here rather than left to its own TTL --
    matching :meth:`~curator.persistence.db_token_store.DbTokenStore.clear`'s unlink behavior. This never
    touches the shared, identity_sub-free catalog tables (``games``, ``game_concepts``, enrichment caches).

    An ``account_deleted`` row is written to ``account_action_log`` *before* the ``app_users`` row is
    removed. That log entry deliberately does not cascade away with the rest of the account -- see
    migration ``0003_account_action_log.sql`` -- so a deleted account's history remains available for the
    log's retention window.

    :returns: 204 on success, whether or not the caller had any data to delete (deletion is idempotent).
    """
    repository: Repository = request.app.state.repository
    redis_adapter = request.app.state.redis_adapter
    audit_repository: AccountActionLogRepository = request.app.state.audit_repository
    await audit_repository.log(claims.sub, ACTION_ACCOUNT_DELETED)
    await repository.delete_user(claims.sub)
    if redis_adapter is not None:
        await redis_adapter.delete(access_token_cache_key(claims.sub))
    return Response(status_code=204)


@router.get("/me/actions", response_model=AccountActionsResponse)
async def get_my_actions(
    request: Request, claims: TokenClaims = Depends(require_verified_caller)
) -> AccountActionsResponse:
    """Return the caller's own account-action history, oldest first.

    Backs the self-service "download my data" claim in the privacy policy -- every high-level action
    Curator has taken on the caller's behalf (link attempts, unlink, library refresh requests, trophy
    fetches, account deletion) is visible here. Rows are retained for the log's retention window even
    after ``DELETE /me`` (see that route's docstring); a caller who has already deleted their account and
    re-links later can still see their prior history via this same endpoint.
    """
    audit_repository: AccountActionLogRepository = request.app.state.audit_repository
    entries: list[AccountActionLogEntry] = await audit_repository.list_for_user(claims.sub)
    return AccountActionsResponse(
        actions=[
            AccountActionResponse(action=entry.action, detail=entry.detail, occurred_at=entry.occurred_at.isoformat())
            for entry in entries
        ]
    )


def _psn_summary(link: LinkRecord) -> PsnSummary:
    """Render a :class:`LinkRecord`'s expirations as the ``/me``/``/psn/link`` response shape."""
    return PsnSummary(
        access_token_expires_at=_iso(link.access_token_expires_at),
        refresh_token_expires_at=_iso(link.refresh_token_expires_at),
    )


def _iso(value: datetime | None) -> str | None:
    """Render a datetime as ISO-8601, or ``None``."""
    return value.isoformat() if value is not None else None
