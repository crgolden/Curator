"""Repository for the mutation-safety wall's pinned test account (``psn_test_accounts``).

Same shape as :class:`curator.persistence.repository.Repository`: one aggregate per repository class,
backed by a shared :class:`~psycopg_pool.AsyncConnectionPool`, raw parameterized SQL.
"""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool


class TestAccountRepository:
    """DAO over ``psn_test_accounts``: one pinned test account per Curator user.

    :param pool: The shared connection pool.
    """

    __test__ = False  # not a pytest test class despite the "Test" prefix

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def get_pinned_account_id(self, identity_sub: str) -> str | None:
        """Return the pinned test account's ``psn_account_id`` for this user, or ``None`` if none is pinned.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :returns: The pinned PSN account id, or ``None``.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT psn_account_id FROM psn_test_accounts WHERE identity_sub = %s", (identity_sub,))
            row = await cur.fetchone()
        return str(row[0]) if row else None

    async def pin(self, identity_sub: str, psn_account_id: str) -> None:
        """Pin ``psn_account_id`` as this user's test account, replacing any previous pin.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :param psn_account_id: The PSN account id to pin.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO psn_test_accounts (identity_sub, psn_account_id, pinned_at)
                VALUES (%s, %s, now())
                ON CONFLICT (identity_sub) DO UPDATE SET
                    psn_account_id = EXCLUDED.psn_account_id,
                    pinned_at = now()
                """,
                (identity_sub, psn_account_id),
            )
