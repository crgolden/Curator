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

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from curator.deps import require_verified_caller
from curator.link_service import AgentFactory
from curator.persistence.crypto import TokenCrypto
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


@router.get("/me", response_model=MeResponse)
async def me(request: Request, claims: TokenClaims = Depends(require_verified_caller)) -> MeResponse:
    """Return the caller's identity plus their PSN link status.

    :returns: ``{"sub", "email", "linked", "psn"}`` where ``psn`` is ``None`` when unlinked, else
        ``{"access_token_expires_at", "refresh_token_expires_at"}`` (ISO-8601 strings, or ``None``).
    """
    repository: Repository = request.app.state.repository
    token_crypto: TokenCrypto = request.app.state.token_crypto
    agent_factory: AgentFactory = request.app.state.agent_factory

    await reverify_link(claims, repository=repository, token_crypto=token_crypto, agent_factory=agent_factory)

    link = await repository.get_link(claims.sub)
    return MeResponse(
        sub=claims.sub,
        email=claims.email,
        linked=link is not None,
        psn=_psn_summary(link) if link is not None else None,
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
