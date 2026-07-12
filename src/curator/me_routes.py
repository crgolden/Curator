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
from typing import Any

from fastapi import APIRouter, Depends, Request

from curator.deps import require_verified_caller
from curator.link_service import AgentFactory
from curator.persistence.crypto import TokenCrypto
from curator.persistence.repository import LinkRecord, Repository
from curator.reverify import reverify_link
from curator.token_validation import TokenClaims

router = APIRouter()


@router.get("/me")
async def me(request: Request, claims: TokenClaims = Depends(require_verified_caller)) -> dict[str, Any]:
    """Return the caller's identity plus their PSN link status.

    :returns: ``{"sub", "email", "linked", "psn"}`` where ``psn`` is ``None`` when unlinked, else
        ``{"access_token_expires_at", "refresh_token_expires_at"}`` (ISO-8601 strings, or ``None``).
    """
    repository: Repository = request.app.state.repository
    token_crypto: TokenCrypto = request.app.state.token_crypto
    agent_factory: AgentFactory = request.app.state.agent_factory

    reverify_link(claims, repository=repository, token_crypto=token_crypto, agent_factory=agent_factory)

    link = repository.get_link(claims.sub)
    return {
        "sub": claims.sub,
        "email": claims.email,
        "linked": link is not None,
        "psn": _psn_summary(link) if link is not None else None,
    }


def _psn_summary(link: LinkRecord) -> dict[str, Any]:
    """Render a :class:`LinkRecord`'s expirations as the ``/me``/``/psn/link`` response shape."""
    return {
        "access_token_expires_at": _iso(link.access_token_expires_at),
        "refresh_token_expires_at": _iso(link.refresh_token_expires_at),
    }


def _iso(value: datetime | None) -> str | None:
    """Render a datetime as ISO-8601, or ``None``."""
    return value.isoformat() if value is not None else None
