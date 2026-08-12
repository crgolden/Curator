"""Data-access layer over ``account_action_log`` -- the defensive audit trail described in migration
``0003_account_action_log.sql``.

Same shape as every other repository in this codebase: backed by a shared
``psycopg_pool.AsyncConnectionPool``, raw parameterized SQL, frozen dataclass results. Deliberately
separate from :class:`curator.persistence.repository.Repository` -- ``account_action_log`` has no foreign
key to ``app_users`` and must keep working (including being written to) after a user's account row is
gone, which is not true of anything else that repository touches.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import AsyncConnectionPool

ACTION_LINK_SUCCEEDED = "link_succeeded"
ACTION_LINK_FAILED = "link_failed"
ACTION_UNLINKED = "unlinked"
ACTION_LIBRARY_REFRESH_REQUESTED = "library_refresh_requested"
ACTION_TROPHY_FETCH = "trophy_fetch"
ACTION_ACCOUNT_DELETED = "account_deleted"
ACTION_ENRICHMENT_KEY_ADDED = "enrichment_key_added"
ACTION_ENRICHMENT_KEY_REMOVED = "enrichment_key_removed"
ACTION_ENRICHMENT_KEY_REJECTED = "enrichment_key_rejected"
ACTION_FOLLOWED = "followed"
ACTION_UNFOLLOWED = "unfollowed"
ACTION_FRIEND_ADDED = "friend_added"
ACTION_FRIEND_REMOVED = "friend_removed"
ACTION_CHAT_GROUP_CREATED = "chat_group_created"
ACTION_CHAT_MESSAGE_SENT = "chat_message_sent"
ACTION_CHAT_MEMBERSHIP_CHANGED = "chat_membership_changed"

PSN_MUTATION_ACTIONS: tuple[str, ...] = (
    ACTION_FRIEND_ADDED,
    ACTION_FRIEND_REMOVED,
    ACTION_CHAT_GROUP_CREATED,
    ACTION_CHAT_MESSAGE_SENT,
    ACTION_CHAT_MEMBERSHIP_CHANGED,
)


@dataclass(frozen=True, slots=True)
class AccountActionLogEntry:
    """One ``account_action_log`` row."""

    log_id: str
    identity_sub: str
    action: str
    detail: str | None
    occurred_at: datetime


class AccountActionLogRepository:
    """DAO over ``account_action_log``.

    :param pool: The shared connection pool.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def log(self, identity_sub: str, action: str, detail: str | None = None) -> None:
        """Record one high-level action taken on ``identity_sub``'s behalf.

        :param identity_sub: The Identity ``sub`` claim of the affected user.
        :param action: One of the ``account_action_log.action`` CHECK values (see the
            ``ACTION_*`` module constants) -- never a raw PSN API call name.
        :param detail: A short human-readable summary (e.g. a failure reason). Must never contain the
            npsso, a token, or raw PSN response data.
        """
        sql = "INSERT INTO account_action_log (identity_sub, action, detail) VALUES (%s, %s, %s)"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (identity_sub, action, detail))

    async def list_for_user(self, identity_sub: str) -> list[AccountActionLogEntry]:
        """Return every logged action for ``identity_sub``, oldest first.

        Backs the user-facing download/export of their own audit history.

        :param identity_sub: The Identity ``sub`` claim of the user requesting their history.
        """
        sql = (
            "SELECT log_id, identity_sub, action, detail, occurred_at FROM account_action_log "
            "WHERE identity_sub = %s ORDER BY occurred_at ASC"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (identity_sub,))
            rows = await cur.fetchall()
        return [
            AccountActionLogEntry(
                log_id=str(row[0]),
                identity_sub=str(row[1]),
                action=row[2],
                detail=row[3],
                occurred_at=row[4],
            )
            for row in rows
        ]

    async def count_since(self, identity_sub: str, actions: tuple[str, ...], since: datetime) -> int:
        """Count this user's logged ``actions`` at or after ``since``.

        Backs the per-user daily cap on PSN mutations -- the audit trail doubles as the meter, since a row
        is written precisely when a mutation succeeded. The count is best-effort in one direction only: a
        failed audit write leaves a real mutation uncounted (``curator.social_routes`` will not fail a
        mutation that already landed on PSN just because the log write did), so the cap can undercount but
        never overcount.

        :param identity_sub: The Identity ``sub`` claim of the affected user.
        :param actions: The action names to count.
        :param since: Only rows with ``occurred_at`` at or after this timestamp are counted.
        """
        sql = (
            "SELECT COUNT(*) FROM account_action_log WHERE identity_sub = %s AND action = ANY(%s) AND occurred_at >= %s"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (identity_sub, list(actions), since))
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def purge_older_than(self, cutoff: datetime) -> int:
        """Delete every row older than ``cutoff``, returning the number of rows removed.

        Called by the retention purge job (see the ``Functions`` repo's timer trigger), never from a
        request path -- nothing in the application itself trims this table on its own.

        :param cutoff: Rows with ``occurred_at`` strictly before this timestamp are deleted.
        """
        sql = "DELETE FROM account_action_log WHERE occurred_at < %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (cutoff,))
            return cur.rowcount
