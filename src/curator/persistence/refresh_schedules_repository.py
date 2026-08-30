"""Data-access layer over ``user_refresh_schedules`` (see ``db/migrations/0031_user_refresh_schedules.sql``).

Curator owns the consent record and the CRUD surface over it. Advancing a schedule after a run, recording
failures and pausing a broken chain belong to the worker that processes the scheduled message, which is not
in this runtime -- see ``AGENTS/Functions.md`` for the message contract.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from psycopg_pool import AsyncConnectionPool

Cadence = Literal["weekly", "monthly"]

_CADENCE_INTERVALS: dict[str, timedelta] = {
    "weekly": timedelta(days=7),
    "monthly": timedelta(days=30),
}


def next_run_after(cadence: Cadence, *, now: datetime | None = None) -> datetime:
    """Return the next run time one ``cadence`` interval from ``now``.

    ``monthly`` is 30 days rather than a calendar month: the cadence exists to bound how stale a library
    gets, and PS Plus's own "first Tuesday" convention is a publishing habit rather than a contract, so a
    few days' drift changes nothing a caller can observe.

    :param cadence: ``"weekly"`` or ``"monthly"``.
    :param now: The instant to measure from; defaults to the current UTC time.
    """
    return (now or datetime.now(timezone.utc)) + _CADENCE_INTERVALS[cadence]


@dataclass(frozen=True, slots=True)
class RefreshSchedule:
    """One user's scheduled-refresh opt-in.

    :param paused_reason: Why the chain stopped, or ``None`` while it is healthy. Set by the worker when a
        schedule stops for a reason the user must act on -- a rejected PSN token needs a manual re-link,
        and repeated failures need attention rather than compounding.
    """

    identity_sub: str
    cadence: Cadence
    ps_plus_watch: bool
    next_run_at: datetime
    last_run_at: datetime | None
    consecutive_failures: int
    paused_reason: str | None


class RefreshSchedulesRepository:
    """DAO over ``user_refresh_schedules``.

    :param pool: The shared connection pool.
    """

    _COLUMNS = "identity_sub, cadence, ps_plus_watch, next_run_at, last_run_at, consecutive_failures, paused_reason"

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def get(self, sub: str) -> RefreshSchedule | None:
        """Return ``sub``'s schedule, or ``None`` if they have not opted in.

        :param sub: The Identity ``sub`` claim.
        """
        sql = f"SELECT {self._COLUMNS} FROM user_refresh_schedules WHERE identity_sub = %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))
            row = await cur.fetchone()
        return self._to_schedule(row) if row is not None else None

    async def upsert(
        self, sub: str, *, cadence: Cadence, ps_plus_watch: bool, next_run_at: datetime
    ) -> RefreshSchedule:
        """Create or replace ``sub``'s schedule, clearing any pause and failure count.

        Re-opting in is the user's answer to whatever paused the chain, so it resets
        ``consecutive_failures`` and ``paused_reason`` rather than leaving a fresh schedule inheriting a
        previous one's failure state.

        :param sub: The Identity ``sub`` claim.
        :param cadence: How often to refresh.
        :param ps_plus_watch: Whether to summarize PS Plus catalog movement on each run.
        :param next_run_at: When the first run should happen.
        """
        sql = (
            "INSERT INTO user_refresh_schedules "
            "(identity_sub, cadence, ps_plus_watch, next_run_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (identity_sub) DO UPDATE SET "
            "cadence = EXCLUDED.cadence, ps_plus_watch = EXCLUDED.ps_plus_watch, "
            "next_run_at = EXCLUDED.next_run_at, consecutive_failures = 0, paused_reason = NULL, "
            "updated_at = now() "
            f"RETURNING {self._COLUMNS}"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub, cadence, ps_plus_watch, next_run_at))
            row = await cur.fetchone()
        assert row is not None, "an upsert with RETURNING always yields the affected row"
        return self._to_schedule(row)

    async def delete(self, sub: str) -> None:
        """Remove ``sub``'s schedule. A no-op when they have none.

        :param sub: The Identity ``sub`` claim.
        """
        sql = "DELETE FROM user_refresh_schedules WHERE identity_sub = %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))

    @staticmethod
    def _to_schedule(row: Sequence[Any]) -> RefreshSchedule:
        return RefreshSchedule(
            identity_sub=str(row[0]),
            cadence=row[1],
            ps_plus_watch=row[2],
            next_run_at=row[3],
            last_run_at=row[4],
            consecutive_failures=row[5],
            paused_reason=row[6],
        )
