"""Data-access layer over the ``app_users`` / ``psn_links`` account tables.

Raw SQL via psycopg 3 rather than an ORM — the schema (``db/migrations/0001_initial.sql``) is small and
deliberate, and this repo already favors ADO.NET-style hand-written SQL over a mapper in its sibling .NET
services. :class:`Repository` takes a ``connection_factory`` rather than opening its own pool so tests can
inject a fake connection with no real database.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import psycopg


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
    """

    psn_account_id: str | None
    token_response_enc: bytes
    access_token_expires_at: datetime | None
    refresh_token_expires_at: datetime | None
    linked_at: datetime
    updated_at: datetime
    last_verified_at: datetime | None


class Repository:
    """DAO over ``app_users`` and ``psn_links``.

    :param connection_factory: A zero-argument callable returning a new ``psycopg.Connection`` (or a
        test fake with the same context-manager/cursor/commit shape). A new connection is opened per
        method call rather than held open, matching the short-lived-request shape of a FastAPI handler.
    """

    def __init__(self, connection_factory: Callable[[], psycopg.Connection]) -> None:
        self._connection_factory = connection_factory

    def upsert_user(self, sub: str) -> None:
        """Insert ``app_users`` row for ``sub`` if absent, else bump ``updated_at``.

        :param sub: The Identity ``sub`` claim (the user's ``identity_sub``).
        """
        sql = (
            "INSERT INTO app_users (identity_sub) VALUES (%s) "
            "ON CONFLICT (identity_sub) DO UPDATE SET updated_at = now()"
        )
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (sub,))
            conn.commit()

    def touch_login(self, sub: str) -> None:
        """Record a login: set ``last_login_at`` (and ``updated_at``) to now.

        :param sub: The Identity ``sub`` claim.
        """
        sql = "UPDATE app_users SET last_login_at = now(), updated_at = now() WHERE identity_sub = %s"
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (sub,))
            conn.commit()

    def get_link(self, sub: str) -> LinkRecord | None:
        """Fetch the ``psn_links`` row for ``sub``, if any.

        :param sub: The Identity ``sub`` claim.
        :returns: The stored :class:`LinkRecord`, or ``None`` if the user has no PSN link.
        """
        sql = (
            "SELECT psn_account_id, token_response_enc, access_token_expires_at, "
            "refresh_token_expires_at, linked_at, updated_at, last_verified_at "
            "FROM psn_links WHERE identity_sub = %s"
        )
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (sub,))
                row = cur.fetchone()
            conn.commit()

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
        )

    def upsert_link(
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
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
            conn.commit()

    def set_link_account(self, sub: str, psn_account_id: str) -> None:
        """Set the linked PSN account id for ``sub``, once it's discovered.

        :param sub: The Identity ``sub`` claim.
        :param psn_account_id: The PSN account id to record.
        """
        sql = "UPDATE psn_links SET psn_account_id = %s, updated_at = now() WHERE identity_sub = %s"
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (psn_account_id, sub))
            conn.commit()

    def touch_link_verified(self, sub: str) -> None:
        """Stamp ``last_verified_at`` to now, recording that the link's email match was just re-checked.

        :param sub: The Identity ``sub`` claim.
        """
        sql = "UPDATE psn_links SET last_verified_at = now() WHERE identity_sub = %s"
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (sub,))
            conn.commit()

    def delete_link(self, sub: str) -> None:
        """Remove the ``psn_links`` row for ``sub`` (unlink the PSN account).

        :param sub: The Identity ``sub`` claim.
        """
        sql = "DELETE FROM psn_links WHERE identity_sub = %s"
        with self._connection_factory() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (sub,))
            conn.commit()
