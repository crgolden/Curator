"""Data-access layer over the ``app_users`` / ``psn_links`` account tables.

Raw SQL via psycopg 3 rather than an ORM — the schema (``db/migrations/0001_initial.sql``) is small and
deliberate, and this repo already favors ADO.NET-style hand-written SQL over a mapper in its sibling .NET
services. :class:`Repository` is backed by a shared ``psycopg_pool.AsyncConnectionPool`` (one pool per
process, opened once in ``create_app()``'s lifespan) rather than opening a connection per call, and every
method is a coroutine so a slow query never blocks the event loop or exhausts FastAPI's sync threadpool.
Tests inject a hand-written fake pool with the same async context-manager/cursor shape, never a real
database or a mocking library.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True)
class LinkRecord:
    """A user's stored PSN link row.

    :param psn_account_id: The linked PSN account id, if known yet.
    :param token_response_enc: The Fernet-encrypted, JSON-serialized PSN token response.
    :param access_token_expires_at: When the current access token expires, if known.
    :param refresh_token_expires_at: When the refresh token expires, if known.
    :param linked_at: When the link was first created.
    :param updated_at: When the link row was last written.
    :param last_verified_at: When the link's email match was last re-verified against a bearer token (see
        ``curator.reverify.reverify_link``), or ``None`` if it has never been (re-)verified since being
        created via :meth:`Repository.upsert_link`/:meth:`Repository.set_link_account`.
    :param harvest_trophies: Whether the user has opted in to Curator harvesting/displaying their PSN
        trophy data. Defaults to ``False`` (opt-in-by-default) — see ``db/migrations/0002_psn_data_preferences.sql``.
    :param harvest_identity: Whether the user has opted in to Curator harvesting/displaying their PSN
        identity data (account id / online id lookups).
    :param harvest_presence: Whether the user has opted in to Curator harvesting/displaying their PSN
        presence data.
    :param harvest_devices: Whether the user has opted in to Curator harvesting/displaying their PSN
        device data.
    """

    psn_account_id: str | None
    token_response_enc: bytes
    access_token_expires_at: datetime | None
    refresh_token_expires_at: datetime | None
    linked_at: datetime
    updated_at: datetime
    last_verified_at: datetime | None
    harvest_trophies: bool = False
    harvest_identity: bool = False
    harvest_presence: bool = False
    harvest_devices: bool = False


class Repository:
    """DAO over ``app_users`` and ``psn_links``.

    :param pool: The shared ``psycopg_pool.AsyncConnectionPool`` (or a test fake with the same
        ``async with pool.connection() as conn`` / ``async with conn.cursor() as cur`` shape). A
        connection is checked out from the pool per method call and returned on exit; the pool's
        connection context manager commits on clean exit and rolls back on exception, matching the
        short-lived-request shape of a FastAPI handler without needing an explicit ``conn.commit()``.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def upsert_user(self, sub: str) -> None:
        """Insert ``app_users`` row for ``sub`` if absent, else bump ``updated_at``.

        :param sub: The Identity ``sub`` claim (the user's ``identity_sub``).
        """
        sql = (
            "INSERT INTO app_users (identity_sub) VALUES (%s) "
            "ON CONFLICT (identity_sub) DO UPDATE SET updated_at = now()"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))

    async def touch_login(self, sub: str) -> None:
        """Record a login: set ``last_login_at`` (and ``updated_at``) to now.

        :param sub: The Identity ``sub`` claim.
        """
        sql = "UPDATE app_users SET last_login_at = now(), updated_at = now() WHERE identity_sub = %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))

    async def get_link(self, sub: str) -> LinkRecord | None:
        """Fetch the ``psn_links`` row for ``sub``, if any.

        :param sub: The Identity ``sub`` claim.
        :returns: The stored :class:`LinkRecord`, or ``None`` if the user has no PSN link.
        """
        sql = (
            "SELECT psn_account_id, token_response_enc, access_token_expires_at, "
            "refresh_token_expires_at, linked_at, updated_at, last_verified_at, "
            "harvest_trophies, harvest_identity, harvest_presence, harvest_devices "
            "FROM psn_links WHERE identity_sub = %s"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))
            row = await cur.fetchone()

        if row is None:
            return None
        return LinkRecord(
            psn_account_id=row[0],
            token_response_enc=row[1],
            access_token_expires_at=row[2],
            refresh_token_expires_at=row[3],
            linked_at=row[4],
            updated_at=row[5],
            last_verified_at=row[6],
            harvest_trophies=row[7],
            harvest_identity=row[8],
            harvest_presence=row[9],
            harvest_devices=row[10],
        )

    async def upsert_link(
        self,
        sub: str,
        token_response_enc: bytes,
        access_token_expires_at: datetime | None,
        refresh_token_expires_at: datetime | None,
        psn_account_id: str | None = None,
    ) -> None:
        """Create or update the ``psn_links`` row for ``sub``.

        ``psn_account_id`` is only overwritten when a non-``None`` value is supplied — a token refresh
        (which doesn't re-discover the account id) must never clobber a previously learned one.

        :param sub: The Identity ``sub`` claim.
        :param token_response_enc: The Fernet-encrypted, JSON-serialized PSN token response.
        :param access_token_expires_at: When the new access token expires, if known.
        :param refresh_token_expires_at: When the refresh token expires, if known.
        :param psn_account_id: The linked PSN account id, if newly discovered this call.
        """
        sql = (
            "INSERT INTO psn_links "
            "(identity_sub, psn_account_id, token_response_enc, access_token_expires_at, "
            "refresh_token_expires_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (identity_sub) DO UPDATE SET "
            "psn_account_id = COALESCE(EXCLUDED.psn_account_id, psn_links.psn_account_id), "
            "token_response_enc = EXCLUDED.token_response_enc, "
            "access_token_expires_at = EXCLUDED.access_token_expires_at, "
            "refresh_token_expires_at = EXCLUDED.refresh_token_expires_at, "
            "updated_at = now()"
        )
        params = (
            sub,
            psn_account_id,
            token_response_enc,
            access_token_expires_at,
            refresh_token_expires_at,
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)

    async def set_link_account(self, sub: str, psn_account_id: str) -> None:
        """Set the linked PSN account id for ``sub``, once it's discovered.

        :param sub: The Identity ``sub`` claim.
        :param psn_account_id: The PSN account id to record.
        """
        sql = "UPDATE psn_links SET psn_account_id = %s, updated_at = now() WHERE identity_sub = %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (psn_account_id, sub))

    async def set_psn_preferences(
        self,
        sub: str,
        *,
        harvest_trophies: bool,
        harvest_identity: bool,
        harvest_presence: bool,
        harvest_devices: bool,
    ) -> None:
        """Set all four PSN data-harvest preference flags for ``sub`` in one atomic update.

        A no-op (0 rows affected, no exception) if the user has no ``psn_links`` row -- callers are
        expected to check :meth:`get_link` first and 404 themselves, matching every other write in this
        class.

        :param sub: The Identity ``sub`` claim.
        :param harvest_trophies: Whether Curator may harvest/display the user's PSN trophy data.
        :param harvest_identity: Whether Curator may harvest/display the user's PSN identity data.
        :param harvest_presence: Whether Curator may harvest/display the user's PSN presence data.
        :param harvest_devices: Whether Curator may harvest/display the user's PSN device data.
        """
        sql = (
            "UPDATE psn_links SET harvest_trophies = %s, harvest_identity = %s, "
            "harvest_presence = %s, harvest_devices = %s WHERE identity_sub = %s"
        )
        params = (harvest_trophies, harvest_identity, harvest_presence, harvest_devices, sub)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)

    async def touch_link_verified(self, sub: str) -> None:
        """Stamp ``last_verified_at`` to now, recording that the link's email match was just re-checked.

        :param sub: The Identity ``sub`` claim.
        """
        sql = "UPDATE psn_links SET last_verified_at = now() WHERE identity_sub = %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))

    async def delete_link(self, sub: str) -> None:
        """Remove the ``psn_links`` row for ``sub`` (unlink the PSN account).

        :param sub: The Identity ``sub`` claim.
        """
        sql = "DELETE FROM psn_links WHERE identity_sub = %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))

    async def delete_user(self, sub: str) -> None:
        """Remove the ``app_users`` row for ``sub`` entirely.

        Every other account-scoped table (``psn_links``, ``entitlement_pulls``, ``library_entries``,
        ``library_exclusions``, ``user_consoles``, ``measured_sizes``, ``collection_definitions``,
        ``collection_runs``, ...) declares ``REFERENCES app_users (identity_sub) ON DELETE CASCADE`` or a
        plain FK cleaned up alongside it, so this one delete wipes everything Curator has ever stored about
        the user. It never touches the shared, identity_sub-free catalog tables (``games``,
        ``game_concepts``, enrichment/cache tables).

        :param sub: The Identity ``sub`` claim of the user requesting deletion.
        """
        sql = "DELETE FROM app_users WHERE identity_sub = %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))
