"""Data-access layer over ``user_enrichment_keys`` -- a user's optionally-provided, encrypted RAWG/
OpenCritic API keys (see ``db/migrations/0004_user_enrichment_keys.sql``).

Deliberately separate from :class:`curator.persistence.repository.Repository` (PSN links) and
:class:`curator.enrichment.repository.EnrichmentRepository` (the shared catalog aggregate) -- same
modular-repository convention used throughout this codebase. Never decrypts anything itself; encryption
happens at the route layer (``curator.enrichment_keys_routes``) and decryption happens at the one place a
key is actually used (``curator.app._library_refresh_handler``), both via the existing
:class:`curator.persistence.crypto.TokenCrypto`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True, slots=True)
class EnrichmentKeyStatus:
    """Boolean/metadata-only view of a user's configured enrichment keys -- never the encrypted bytes."""

    rawg_configured: bool
    opencritic_configured: bool
    rawg_added_at: datetime | None
    opencritic_added_at: datetime | None


class EnrichmentKeysRepository:
    """DAO over ``user_enrichment_keys``.

    :param pool: The shared connection pool.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def get_status(self, sub: str) -> EnrichmentKeyStatus:
        """Return whether ``sub`` has a RAWG/OpenCritic key configured, and when each was added.

        Always answerable -- a user with no row (never configured either provider) gets both flags
        ``False``, never a 404; this is a status check, not a resource lookup.

        :param sub: The Identity ``sub`` claim.
        """
        sql = (
            "SELECT rawg_api_key_enc, opencritic_api_key_enc, rawg_added_at, opencritic_added_at "
            "FROM user_enrichment_keys WHERE identity_sub = %s"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))
            row = await cur.fetchone()

        if row is None:
            return EnrichmentKeyStatus(
                rawg_configured=False, opencritic_configured=False, rawg_added_at=None, opencritic_added_at=None
            )
        return EnrichmentKeyStatus(
            rawg_configured=row[0] is not None,
            opencritic_configured=row[1] is not None,
            rawg_added_at=row[2],
            opencritic_added_at=row[3],
        )

    async def get_decrypted_key_material(self, sub: str) -> tuple[bytes | None, bytes | None]:
        """Return ``(rawg_api_key_enc, opencritic_api_key_enc)`` -- still encrypted, despite the name
        describing what the caller will do with them next.

        Internal use only (``curator.app._library_refresh_handler``, which holds the ``TokenCrypto``
        needed to actually decrypt these). Never exposed through a route.

        :param sub: The Identity ``sub`` claim.
        """
        sql = "SELECT rawg_api_key_enc, opencritic_api_key_enc FROM user_enrichment_keys WHERE identity_sub = %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))
            row = await cur.fetchone()
        if row is None:
            return None, None
        return row[0], row[1]

    async def upsert_rawg_key(self, sub: str, key_enc: bytes) -> None:
        """Create or update ``sub``'s RAWG key, leaving any OpenCritic key untouched.

        :param sub: The Identity ``sub`` claim.
        :param key_enc: The Fernet-encrypted API key.
        """
        sql = (
            "INSERT INTO user_enrichment_keys (identity_sub, rawg_api_key_enc, rawg_added_at) "
            "VALUES (%s, %s, now()) "
            "ON CONFLICT (identity_sub) DO UPDATE SET "
            "rawg_api_key_enc = EXCLUDED.rawg_api_key_enc, rawg_added_at = now(), updated_at = now()"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub, key_enc))

    async def upsert_opencritic_key(self, sub: str, key_enc: bytes) -> None:
        """Create or update ``sub``'s OpenCritic key, leaving any RAWG key untouched.

        :param sub: The Identity ``sub`` claim.
        :param key_enc: The Fernet-encrypted API key.
        """
        sql = (
            "INSERT INTO user_enrichment_keys (identity_sub, opencritic_api_key_enc, opencritic_added_at) "
            "VALUES (%s, %s, now()) "
            "ON CONFLICT (identity_sub) DO UPDATE SET "
            "opencritic_api_key_enc = EXCLUDED.opencritic_api_key_enc, opencritic_added_at = now(), "
            "updated_at = now()"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub, key_enc))

    async def delete_rawg_key(self, sub: str) -> None:
        """Clear ``sub``'s RAWG key, leaving any OpenCritic key (and the row itself) intact.

        :param sub: The Identity ``sub`` claim.
        """
        sql = (
            "UPDATE user_enrichment_keys SET rawg_api_key_enc = NULL, rawg_added_at = NULL, "
            "updated_at = now() WHERE identity_sub = %s"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))

    async def delete_opencritic_key(self, sub: str) -> None:
        """Clear ``sub``'s OpenCritic key, leaving any RAWG key (and the row itself) intact.

        :param sub: The Identity ``sub`` claim.
        """
        sql = (
            "UPDATE user_enrichment_keys SET opencritic_api_key_enc = NULL, opencritic_added_at = NULL, "
            "updated_at = now() WHERE identity_sub = %s"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))
