"""A PostgreSQL-backed PSN token store, satisfying psnpy's ``TokenStore`` contract.

``psnpy.auth.TokenStore`` persists a PSN token response (access + refresh tokens and their expiry
timestamps) to a JSON file on disk — fine for a single-user CLI, wrong for a multi-user API where each
Identity-authenticated user needs their own persisted token, encrypted at rest. :class:`DbTokenStore`
implements the same ``load`` / ``save`` / ``clear`` shape psnpy's ``PsnAgent`` expects, backed by the
``psn_links`` table instead of a file — so a ``PsnAgent`` can be handed a ``DbTokenStore`` in place of a
``TokenStore`` with no code change on the psnpy side. This module deliberately does **not** import
``psnpy``: the contract is satisfied structurally (duck typing), keeping persistence decoupled from the
PSN client library.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

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

    def load(self) -> Optional[dict[str, Any]]:
        """Load the cached token response, or ``None`` if absent, corrupt, or unusable.

        :returns: The token response dict, only when it has a truthy ``refresh_token``; ``None``
            otherwise (no row, decryption failure, or a token response missing a refresh token).
        """
        link = self._repository.get_link(self._sub)
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

        return data if isinstance(data, dict) and data.get("refresh_token") else None

    def save(self, token_response: dict[str, Any]) -> None:
        """Persist a token response, replacing any previous value.

        Mirrors psnpy's ``TokenStore.load`` contract: only a token response with a truthy
        ``refresh_token`` is worth persisting, so anything else is silently ignored.

        :param token_response: The PSN token response dict (as produced by ``psnpy.psn_api.PsnSession``).
            Its ``access_token_expires_at`` / ``refresh_token_expires_at`` keys, when present, hold
            precomputed absolute Unix epoch timestamps.
        """
        if not isinstance(token_response, dict) or not token_response.get("refresh_token"):
            return

        encrypted = self._crypto.encrypt(json.dumps(token_response).encode("utf-8"))
        access_expires = _to_datetime(token_response.get("access_token_expires_at"))
        refresh_expires = _to_datetime(token_response.get("refresh_token_expires_at"))

        self._repository.upsert_link(
            self._sub,
            encrypted,
            access_expires,
            refresh_expires,
        )

    def clear(self) -> None:
        """Remove the cached token, if present."""
        self._repository.delete_link(self._sub)


def _to_datetime(value: Any) -> Optional[datetime]:
    """Convert a Unix epoch (seconds) to a timezone-aware UTC ``datetime``.

    :param value: An epoch timestamp (``int``/``float``), or ``None``.
    :returns: The corresponding aware ``datetime``, or ``None`` if ``value`` is ``None``.
    """
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)
