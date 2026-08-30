"""Repository for ``job_runs`` -- the status-tracking counterpart to a queued library-refresh/enrichment
job, so ``GET /library/refresh/{run_id}`` has something to poll.

Same shape as every other repository in this codebase: backed by a shared
:class:`~psycopg_pool.AsyncConnectionPool`, raw parameterized SQL, frozen dataclass results.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from psycopg_pool import AsyncConnectionPool

TERMINAL_STATUSES: Final[tuple[str, ...]] = ("succeeded", "failed", "cancelled")
"""Every ``job_runs.status`` a run can never leave.

Non-terminal is expressed as the complement of this set, never as its own list, so a status added to
``job_runs_status_check`` (``0046``) can only be non-terminal by omission from here -- the failure mode
worth designing out is a new terminal status that every "is this run still active" predicate keeps
reporting as active.
"""

_NOT_TERMINAL_SQL: Final = "status <> ALL(%s)"


@dataclass(frozen=True, slots=True)
class JobRun:
    """One ``job_runs`` row."""

    run_id: str
    kind: str
    identity_sub: str | None
    status: str
    error: str | None
    result_summary: dict[str, Any] | None
    updated_at: datetime
    lease_expires_at: datetime | None = None


class JobRunsRepository:
    """DAO over ``job_runs``.

    :param pool: The shared connection pool.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def create(self, run_id: str, kind: str, identity_sub: str | None = None) -> None:
        """Insert a new ``job_runs`` row in ``queued`` status.

        :param run_id: The run id (already generated client-side by
            :class:`~curator.jobs.queue_publisher.QueuePublisher`).
        :param kind: ``"library_refresh"`` or ``"enrichment"``.
        :param identity_sub: The Curator user id (Identity's ``sub``) this run is for; ``None`` for an
            ``"enrichment"`` run (a global, admin-scoped re-scrape, not per-user).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO job_runs (run_id, kind, identity_sub) VALUES (%s, %s, %s)",
                (run_id, kind, identity_sub),
            )

    async def mark_failed(self, run_id: str, error: str) -> None:
        """Transition a run to ``failed``, recording ``error``."""
        await self._set_status(run_id, "failed", error=error)

    async def _set_status(self, run_id: str, status: str, *, error: str | None = None) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE job_runs SET status = %s, error = %s, updated_at = now(), lease_expires_at = NULL "
                "WHERE run_id = %s",
                (status, error, run_id),
            )

    async def get(self, run_id: str) -> JobRun | None:
        """Return one run, or ``None`` if ``run_id`` is unknown."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT run_id, kind, identity_sub, status, error, result_summary, updated_at, lease_expires_at "
                "FROM job_runs WHERE run_id = %s",
                (run_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return self._row_to_job_run(row)

    async def cancel(self, run_id: str, reason: str) -> bool:
        """Stand a non-terminal run down, recording ``reason`` in ``error``.

        The row is kept rather than deleted -- ``job_runs`` is the audit trail ``ExpiredLeaseReaper`` and
        every operator query read. The lease is cleared so nothing renews it, and the guard is the
        non-terminal predicate, so cancelling an already-``succeeded`` run cannot rewrite its outcome.

        :param run_id: The run to cancel.
        :param reason: Operator-visible text stored in ``error``.
        :returns: ``True`` if a run moved to ``cancelled``, ``False`` if ``run_id`` is unknown or already
            terminal.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE job_runs SET status = 'cancelled', error = %s, updated_at = now(), "
                f"lease_expires_at = NULL WHERE run_id = %s AND {_NOT_TERMINAL_SQL}",
                (reason, run_id, list(TERMINAL_STATUSES)),
            )
            return bool(cur.rowcount)

    async def find_active_run(self, identity_sub: str, kind: str) -> JobRun | None:
        """Return the caller's own most recent non-terminal (``queued``/``running``/``rate_limited``) run
        of this kind, or ``None`` if none exists.

        Reports only what exists; whether a returned run is still alive is the caller's decision, and
        :class:`JobRun` carries both ``lease_expires_at`` and ``updated_at`` for it.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT run_id, kind, identity_sub, status, error, result_summary, updated_at, lease_expires_at "
                "FROM job_runs WHERE identity_sub = %s AND kind = %s "
                f"AND {_NOT_TERMINAL_SQL} "
                "ORDER BY created_at DESC LIMIT 1",
                (identity_sub, kind, list(TERMINAL_STATUSES)),
            )
            row = await cur.fetchone()
        return self._row_to_job_run(row) if row is not None else None

    async def find_active_global_run(self, kind: str) -> JobRun | None:
        """Return the most recent non-terminal (``queued``/``running``/``rate_limited``) run of a global
        (``identity_sub IS NULL``) kind, or ``None`` if none exists.

        The ``identity_sub``-less counterpart to :meth:`find_active_run`; staleness is likewise left to
        the caller.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT run_id, kind, identity_sub, status, error, result_summary, updated_at, lease_expires_at "
                "FROM job_runs WHERE identity_sub IS NULL AND kind = %s "
                f"AND {_NOT_TERMINAL_SQL} "
                "ORDER BY created_at DESC LIMIT 1",
                (kind, list(TERMINAL_STATUSES)),
            )
            row = await cur.fetchone()
        return self._row_to_job_run(row) if row is not None else None

    async def get_latest_by_kind(self, kind: str) -> JobRun | None:
        """Return the most recently *queued* run of this kind, or ``None`` if none exists yet.

        Ordered by ``created_at``, not ``updated_at``.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT run_id, kind, identity_sub, status, error, result_summary, updated_at, lease_expires_at "
                "FROM job_runs WHERE kind = %s ORDER BY created_at DESC LIMIT 1",
                (kind,),
            )
            row = await cur.fetchone()
        return self._row_to_job_run(row) if row is not None else None

    @staticmethod
    def _row_to_job_run(row: tuple[Any, ...]) -> JobRun:
        return JobRun(
            run_id=str(row[0]),
            kind=row[1],
            identity_sub=str(row[2]) if row[2] is not None else None,
            status=row[3],
            error=row[4],
            result_summary=row[5],
            updated_at=row[6],
            lease_expires_at=row[7],
        )
