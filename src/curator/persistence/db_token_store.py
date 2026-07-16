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

**Split storage.** Only the ``access_token`` (and its own ``expires_in``/``access_token_expires_at``) is
ephemeral -- it's worthless again in about an hour regardless of use. The ``refresh_token`` is the actual
durable secret (it doesn't rotate on use -- see ``curator.psn.session``'s module docstring) and is what
must survive a process restart. So the encrypted Postgres blob holds everything *except* the ephemeral
access-token fields, and the current access token is cached in Redis instead, keyed by ``sub``, with its
TTL set to its own real remaining lifetime. This avoids re-writing the same encrypted row on every hourly
refresh just to restate an access token nobody will read again after it expires, and keeps the one secret
that actually needs multi-day durability in the one place designed for it. When Redis has no cached access
token (cold cache, eviction, or Redis not configured at all), :meth:`load` still returns the durable
refresh-token dict as-is; ``PsnSession``'s own ``_ensure_fresh``/``_refresh`` logic (unchanged) then
transparently mints a fresh access token from the refresh token on the next call, exactly as it would for
any other expired access token.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Protocol

from cryptography.fernet import InvalidToken

from curator.persistence.crypto import TokenCrypto
from curator.persistence.repository import Repository

_EPHEMERAL_KEYS = frozenset({"access_token", "expires_in", "access_token_expires_at"})


class RedisLike(Protocol):
    """The narrow slice of ``redis.asyncio.Redis``'s string API this cache needs.

    Declared locally (rather than importing ``curator.psn.trophy_cache``'s protocol of the same shape) so
    this module keeps importing nothing from ``curator.psn``, per the module docstring above.
    """

    async def get(self, name: str) -> bytes | str | None:
        """Return the cached value, or ``None`` if absent/expired."""
        ...

    async def set(self, name: str, value: str, ex: int | None = None) -> Any:
        """Set a value with an optional TTL (seconds)."""
        ...

    async def delete(self, name: str) -> Any:
        """Remove a key, if present."""
        ...


def access_token_cache_key(sub: str) -> str:
    return f"curator:psn:access_token:{sub}"


class DbTokenStore:
    """Persists one user's PSN refresh token durably in ``psn_links``, and caches their current access
    token in Redis (see the module docstring for why the two are split).

    :param sub: The Identity ``sub`` claim identifying the user whose token this store manages.
    :param repository: The :class:`~curator.persistence.repository.Repository` to read/write through.
    :param crypto: The :class:`~curator.persistence.crypto.TokenCrypto` used to encrypt/decrypt the
        stored token bytes.
    :param redis: The shared Redis client backing the access-token cache; ``None`` disables caching --
        every restore then refreshes the access token via the durable refresh token instead.
    """

    def __init__(self, sub: str, repository: Repository, crypto: TokenCrypto, redis: RedisLike | None = None) -> None:
        self._sub = sub
        self._repository = repository
        self._crypto = crypto
        self._redis = redis

    async def load(self) -> dict[str, Any] | None:
        """Load the cached token response, or ``None`` if there is no link at all.

        :returns: The durable (refresh-token) fields merged with the cached access-token fields when a
            live one is in Redis, or the durable fields alone otherwise. ``None`` only when there is no
            ``psn_links`` row, or its ciphertext is corrupt/undecryptable.
        """
        link = await self._repository.get_link(self._sub)
        if link is None:
            return None

        try:
            plaintext = self._crypto.decrypt(link.token_response_enc)
        except InvalidToken:
            return None

        try:
            durable = json.loads(plaintext)
        except json.JSONDecodeError:
            return None

        if not isinstance(durable, dict):
            return None

        cached = await self._load_cached_access_token()
        return {**durable, **cached} if cached is not None else durable

    async def save(self, token_response: dict[str, Any]) -> None:
        """Persist a token response, replacing any previous value.

        Only a token response with a truthy ``access_token`` is worth persisting; anything else is
        silently ignored. A missing ``refresh_token`` is fine -- the session remains usable until
        ``access_token_expires_at``, after which reverification will surface the need for a fresh npsso.

        :param token_response: The PSN token response dict (as produced by ``curator.psn.session``).
            Its ``access_token_expires_at`` / ``refresh_token_expires_at`` keys, when present, hold
            precomputed absolute Unix epoch timestamps.
        """
        if not isinstance(token_response, dict) or not token_response.get("access_token"):
            return

        durable = {key: value for key, value in token_response.items() if key not in _EPHEMERAL_KEYS}
        encrypted = self._crypto.encrypt(json.dumps(durable).encode("utf-8"))
        access_expires = _to_datetime(token_response.get("access_token_expires_at"))
        refresh_expires = _to_datetime(token_response.get("refresh_token_expires_at"))

        await self._repository.upsert_link(
            self._sub,
            encrypted,
            access_expires,
            refresh_expires,
        )
        await self._cache_access_token(token_response)

    async def clear(self) -> None:
        """Remove the cached token, if present -- both the durable row and any cached access token."""
        await self._repository.delete_link(self._sub)
        if self._redis is not None:
            await self._redis.delete(access_token_cache_key(self._sub))

    async def _cache_access_token(self, token_response: dict[str, Any]) -> None:
        """Cache the ephemeral access-token fields in Redis, TTL'd to the access token's real remaining
        lifetime. No-ops when Redis isn't configured, or the expiry is unknown/already past."""
        if self._redis is None:
            return

        expires_at = token_response.get("access_token_expires_at")
        if expires_at is None:
            return

        ttl_seconds = int(expires_at - time.time())
        if ttl_seconds <= 0:
            return

        ephemeral = {key: value for key, value in token_response.items() if key in _EPHEMERAL_KEYS}
        await self._redis.set(access_token_cache_key(self._sub), json.dumps(ephemeral), ex=ttl_seconds)

    async def _load_cached_access_token(self) -> dict[str, Any] | None:
        """Load the cached ephemeral access-token fields from Redis, or ``None`` if absent/expired/corrupt
        or Redis isn't configured."""
        if self._redis is None:
            return None

        cached = await self._redis.get(access_token_cache_key(self._sub))
        if cached is None:
            return None

        try:
            data = json.loads(cached if isinstance(cached, str) else cached.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

        return data if isinstance(data, dict) else None


def _to_datetime(value: Any) -> datetime | None:
    """Convert a Unix epoch (seconds) to a timezone-aware UTC ``datetime``.

    :param value: An epoch timestamp (``int``/``float``), or ``None``.
    :returns: The corresponding aware ``datetime``, or ``None`` if ``value`` is ``None``.
    """
    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)
