"""Repository for ``job_runs`` -- the status-tracking counterpart to a queued library-refresh/enrichment
job, so ``GET /library/refresh/{run_id}`` has something to poll.

Same shape as every other repository in this codebase: backed by a shared
:class:`~psycopg_pool.AsyncConnectionPool`, raw parameterized SQL, frozen dataclass results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True, slots=True)
class JobRun:
    """One ``job_runs`` row."""

    run_id: str
    kind: str
    identity_sub: str | None
    status: str
    error: str | None
    result_summary: dict[str, Any] | None


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

    async def mark_running(self, run_id: str) -> None:
        """Transition a run to ``running``."""
        await self._set_status(run_id, "running")

    async def mark_succeeded(self, run_id: str, result_summary: dict[str, Any] | None = None) -> None:
        """Transition a run to ``succeeded``, optionally recording a JSON summary of what it did.

        :param result_summary: E.g. newly-enriched titles per provider (see
            ``curator.library.library_build_orchestrator.LibraryBuildResult``); ``None`` for runs with
            nothing to summarize (an ``"enrichment"`` admin re-scrape, or a fake in tests).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE job_runs SET status = %s, result_summary = %s, updated_at = now() WHERE run_id = %s",
                ("succeeded", json.dumps(result_summary) if result_summary is not None else None, run_id),
            )

    async def mark_failed(self, run_id: str, error: str) -> None:
        """Transition a run to ``failed``, recording ``error``."""
        await self._set_status(run_id, "failed", error=error)

    async def _set_status(self, run_id: str, status: str, *, error: str | None = None) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE job_runs SET status = %s, error = %s, updated_at = now() WHERE run_id = %s",
                (status, error, run_id),
            )

    async def get(self, run_id: str) -> JobRun | None:
        """Return one run, or ``None`` if ``run_id`` is unknown."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT run_id, kind, identity_sub, status, error, result_summary FROM job_runs WHERE run_id = %s",
                (run_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return JobRun(
            run_id=str(row[0]),
            kind=row[1],
            identity_sub=str(row[2]) if row[2] is not None else None,
            status=row[3],
            error=row[4],
            result_summary=row[5],
        )
