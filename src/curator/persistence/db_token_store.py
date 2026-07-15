"""A PostgreSQL-backed PSN token store, satisfying the folded-in ``curator.psn`` package's ``TokenStore``
contract.

The folded-in ``psn/session.py`` persists a PSN token response (access + refresh tokens and their expiry
timestamps) via a duck-typed ``load`` / ``save`` / ``clear`` contract — a JSON-file-backed implementation
is fine for a single-user CLI, wrong for a multi-user API where each Identity-authenticated user needs
their own persisted token, encrypted at rest. :class:`DbTokenStore` implements that same shape, backed by
the ``psn_links`` table instead of a file, so a ``PsnSession`` can be handed a ``DbTokenStore`` in place
of a file-backed store with no code change on the PSN-session side. This module deliberately does **not**
import anything from ``curator.psn``: the contract is satisfied structurally (duck typing), keeping
persistence decoupled from the PSN client code.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import InvalidToken

from curator.persistence.crypto import TokenCrypto
from curator.persistence.repository import Repository


class DbTokenStore:
    """Persists one user's PSN token response as an encrypted row in ``psn_links``.

    :param sub: The Identity ``sub`` claim identifying the user whose token this store manages.
    :param repository: The :class:`~curator.persistence.repository.Repository` to read/write through.
    :param crypto: The :class:`~curator.persistence.crypto.TokenCrypto` used to encrypt/decrypt the
        stored token bytes.
    """

    def __init__(self, sub: str, repository: Repository, crypto: TokenCrypto) -> None:
        self._sub = sub
        self._repository = repository
        self._crypto = crypto

    async def load(self) -> dict[str, Any] | None:
        """Load the cached token response, or ``None`` if absent, corrupt, or unusable.

        :returns: The token response dict, only when it has a truthy ``access_token``; ``None``
            otherwise (no row, decryption failure, or a token response missing an access token).
        """
        link = await self._repository.get_link(self._sub)
        if link is None:
            return None

        try:
            plaintext = self._crypto.decrypt(link.token_response_enc)
        except InvalidToken:
            return None

        try:
            data = json.loads(plaintext)
        except json.JSONDecodeError:
            return None

        return data if isinstance(data, dict) and data.get("access_token") else None

    async def save(self, token_response: dict[str, Any]) -> None:
        """Persist a token response, replacing any previous value.

        Mirrors the ``TokenStore`` contract's ``load`` expectation: only a token response with a truthy
        ``access_token`` is worth persisting, so anything else is silently ignored. A missing
        ``refresh_token`` is fine -- the session remains usable until ``access_token_expires_at``, after
        which reverification will surface the need for a fresh npsso.

        :param token_response: The PSN token response dict (as produced by ``curator.psn.session``).
            Its ``access_token_expires_at`` / ``refresh_token_expires_at`` keys, when present, hold
            precomputed absolute Unix epoch timestamps.
        """
        if not isinstance(token_response, dict) or not token_response.get("access_token"):
            return

        encrypted = self._crypto.encrypt(json.dumps(token_response).encode("utf-8"))
        access_expires = _to_datetime(token_response.get("access_token_expires_at"))
        refresh_expires = _to_datetime(token_response.get("refresh_token_expires_at"))

        await self._repository.upsert_link(
            self._sub,
            encrypted,
            access_expires,
            refresh_expires,
        )

    async def clear(self) -> None:
        """Remove the cached token, if present."""
        await self._repository.delete_link(self._sub)


def _to_datetime(value: Any) -> datetime | None:
    """Convert a Unix epoch (seconds) to a timezone-aware UTC ``datetime``.

    :param value: An epoch timestamp (``int``/``float``), or ``None``.
    :returns: The corresponding aware ``datetime``, or ``None`` if ``value`` is ``None``.
    """
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)
