"""Linking (and unlinking) a user's PSN account to their Curator identity.

Pure logic over injected collaborators (``repository``, ``token_crypto``, ``agent_factory``) — no FastAPI
imports here, so the linking rules can be unit tested without spinning up routes. The central tenet,
enforced throughout: a link is only ever created between two *verified-matching* emails — the caller's
bearer-token ``email`` claim (see ``curator.token_validation.TokenClaims``) and the PSN account's own
verified email. Neither email, nor the npsso used to establish the link, is ever persisted or logged; only
the resulting PSN account id and encrypted tokens are. Any case that fails that match clears whatever
tokens were just obtained, so a rejected link never leaves live PSN credentials sitting in the database.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from psnpy.config import ConfigError as NpssoConfigError
from psnpy.config import parse_npsso
from psnpy.psn_api import PsnAuthError

from curator.persistence.crypto import TokenCrypto
from curator.persistence.db_token_store import DbTokenStore
from curator.persistence.repository import Repository


class LinkError(Exception):
    """Raised when linking a PSN account cannot proceed, carrying which case it was.

    :param kind: One of ``"invalid_npsso"``, ``"mismatch"``, ``"unverified"``, ``"auth_failed"`` — the
        route layer maps each to its own HTTP status/detail.
    :param message: A human-readable explanation (never includes the npsso or an email address).
    """

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class _Account(Protocol):
    """The shape of the account object :meth:`PsnAgentLike.whoami` returns (structural — see psnpy.client.Account)."""

    account_id: str


class PsnAgentLike(Protocol):
    """The shape an injected PSN agent must satisfy: ``whoami()`` + ``account_email_verified()``."""

    def whoami(self) -> _Account:
        """Return the authenticated PSN account (bootstrapping/persisting tokens as a side effect)."""
        ...

    def account_email_verified(self) -> tuple[str, bool] | None:
        """Return ``(address, is_verified)`` for the PSN account's primary email, or ``None``."""
        ...


AgentFactory = Callable[..., PsnAgentLike]


@dataclass(frozen=True)
class LinkResult:
    """The outcome of a successful :func:`link` call.

    :param psn_account_id: The linked PSN account id.
    :param access_token_expires_at: When the current access token expires, if known.
    :param refresh_token_expires_at: When the refresh token expires, if known.
    """

    psn_account_id: str
    access_token_expires_at: datetime | None
    refresh_token_expires_at: datetime | None


def normalize_email(value: str) -> str:
    """Normalize an email address for comparison: strip surrounding whitespace, lowercase.

    :param value: The raw address.
    :returns: The normalized address.
    """
    return value.strip().lower()


def emails_match(identity_email: str, psn_email: str, psn_verified: bool) -> bool:
    """Decide whether an Identity email and a PSN email count as the same, verified, address.

    :param identity_email: The email established at OIDC login.
    :param psn_email: The PSN account's primary email address.
    :param psn_verified: Whether PSN reports that address as verified.
    :returns: ``True`` only if the normalized addresses are equal AND ``psn_verified`` is exactly ``True``.
    """
    return psn_verified is True and normalize_email(identity_email) == normalize_email(psn_email)


def link(
    sub: str,
    npsso: str,
    identity_email: str,
    *,
    repository: Repository,
    token_crypto: TokenCrypto,
    agent_factory: AgentFactory,
) -> LinkResult:
    """Link a user's PSN account, requiring their PSN email to match their verified Identity email.

    Flow: validate ``npsso`` -> build an agent bound to it -> ``whoami()`` (bootstraps and persists tokens
    via the injected token store as a side effect) -> ``account_email_verified()`` -> compare. On any
    failure to match, the just-persisted tokens are cleared immediately — a rejected link never leaves
    live PSN credentials in the database. On a verified match, ``last_verified_at`` is stamped immediately
    (see :meth:`~curator.persistence.repository.Repository.touch_link_verified`) so a token presented right
    after linking doesn't trigger a redundant re-verify.

    :param sub: The Identity ``sub`` claim of the user linking their account.
    :param npsso: The npsso cookie/token supplied by the user.
    :param identity_email: The user's email, from their bearer token's ``email`` claim (never persisted).
    :param repository: The :class:`~curator.persistence.repository.Repository` to read/write through.
    :param token_crypto: The :class:`~curator.persistence.crypto.TokenCrypto` used to clear failed links.
    :param agent_factory: Builds the PSN agent for this ``sub`` given the supplied ``npsso``.
    :returns: The :class:`LinkResult` on a verified match.
    :raises LinkError: ``"invalid_npsso"`` if ``npsso`` fails validation (no agent call is made);
        ``"auth_failed"`` if PSN authentication fails; ``"mismatch"``/``"unverified"`` if the emails don't
        both verify-match.
    """
    try:
        parse_npsso(npsso)
    except NpssoConfigError as exc:
        raise LinkError("invalid_npsso", str(exc)) from exc

    agent = agent_factory(sub, npsso=npsso)

    try:
        account = agent.whoami()
        email_info = agent.account_email_verified()
    except PsnAuthError as exc:
        DbTokenStore(sub, repository, token_crypto).clear()
        raise LinkError("auth_failed", "PSN authentication failed.") from exc

    if email_info is None:
        DbTokenStore(sub, repository, token_crypto).clear()
        raise LinkError("unverified", "PSN email is not verified.")

    psn_email, psn_verified = email_info
    if not psn_verified:
        DbTokenStore(sub, repository, token_crypto).clear()
        raise LinkError("unverified", "PSN email is not verified.")
    if normalize_email(identity_email) != normalize_email(psn_email):
        DbTokenStore(sub, repository, token_crypto).clear()
        raise LinkError("mismatch", "emails do not match")

    repository.set_link_account(sub, account.account_id)
    repository.touch_link_verified(sub)
    link_record = repository.get_link(sub)
    return LinkResult(
        psn_account_id=account.account_id,
        access_token_expires_at=link_record.access_token_expires_at if link_record else None,
        refresh_token_expires_at=link_record.refresh_token_expires_at if link_record else None,
    )


def unlink(sub: str, *, repository: Repository, token_crypto: TokenCrypto) -> None:
    """Unlink a user's PSN account: best-effort revoke, then clear the stored tokens.

    psnpy exposes no token-revocation API (its ``PsnSession``/``PsnAgent`` only bootstrap, refresh, and use
    tokens — there is no PSN endpoint call to invalidate one server-side), so there is nothing to revoke
    here; this simply clears Curator's own copy. Documented explicitly so a future psnpy revoke capability
    has an obvious place to plug in.

    :param sub: The Identity ``sub`` claim of the user unlinking their account.
    :param repository: The :class:`~curator.persistence.repository.Repository` to write through.
    :param token_crypto: The :class:`~curator.persistence.crypto.TokenCrypto` used by the token store.
    """
    DbTokenStore(sub, repository, token_crypto).clear()
