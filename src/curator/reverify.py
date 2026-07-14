"""Re-verifying a stored PSN link against the caller's current access token.

Curator's OIDC login/callback is gone -- it's a pure JWT Bearer resource server now (see ``README.md``) --
so the old "re-verify a stored link on every login" tenet becomes "re-verify whenever a token issued after
the link's last verification arrives". :func:`reverify_link` is that check, shared by ``GET /me`` and
``DELETE /psn/link``. ``POST /psn/link`` doesn't call it: it performs its own full match-and-link check
(see :func:`curator.link_service.link`) and calls
:meth:`~curator.persistence.repository.Repository.touch_link_verified` itself on a successful link.

Identity accounts can change their email, and a PSN account can be re-linked to a different email
elsewhere, so a stale match must not silently keep working forever. This mirrors ``link_service``'s hard
privacy tenet: no email is ever persisted or logged here, only compared in memory.
"""

from __future__ import annotations

from curator.link_service import AgentFactory, normalize_email
from curator.persistence.crypto import TokenCrypto
from curator.persistence.db_token_store import DbTokenStore
from curator.persistence.repository import Repository
from curator.psn.errors import PsnAuthError
from curator.token_validation import TokenClaims


async def reverify_link(
    claims: TokenClaims,
    *,
    repository: Repository,
    token_crypto: TokenCrypto,
    agent_factory: AgentFactory,
) -> None:
    """Re-check the caller's stored PSN link against ``claims``, if it hasn't been checked since this token
    was issued.

    No-ops when there is no stored link at all, or when the link was already verified against a token
    issued no earlier than ``claims.iat`` (an older, or same-vintage, token being re-presented must not
    repeatedly re-hit PSN). Otherwise: clears the link (auto-unlink; every other row is preserved) when PSN
    reports no email, an unverified email, or a mismatched email, and when PSN authentication itself fails
    (the stored tokens are dead anyway). Any other exception -- a network blip, PSN being briefly
    unreachable, ... -- is swallowed and the link is left intact; the next re-check (next newer token)
    tries again.

    :param claims: The validated caller. Callers of this function are expected to have already enforced
        ``claims.email is not None`` (see :func:`curator.deps.require_verified_caller`) -- a route that
        hasn't done so must not call this.
    :param repository: The :class:`~curator.persistence.repository.Repository` to read/write through.
    :param token_crypto: The :class:`~curator.persistence.crypto.TokenCrypto` used to clear a stale link.
    :param agent_factory: Builds the PSN agent for this ``sub``.
    """
    link = await repository.get_link(claims.sub)
    if link is None:
        return

    if link.last_verified_at is not None and claims.iat <= link.last_verified_at:
        return

    try:
        agent = await agent_factory(claims.sub)
        email_info = await agent.account_email_verified()
    except PsnAuthError:
        await DbTokenStore(claims.sub, repository, token_crypto).clear()
        return
    except Exception:
        return

    # Callers must have already enforced `claims.email is not None` (see this function's docstring and
    # `curator.deps.require_verified_caller`) -- narrow it here so normalize_email(claims.email) type-checks.
    assert claims.email is not None, "reverify_link requires a verified caller (claims.email must be set)"

    stale = (
        email_info is None
        or email_info[1] is not True
        or normalize_email(email_info[0]) != normalize_email(claims.email)
    )
    if stale:
        await DbTokenStore(claims.sub, repository, token_crypto).clear()
    else:
        await repository.touch_link_verified(claims.sub)
