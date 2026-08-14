"""Repository for ``job_runs`` -- the status-tracking counterpart to a queued library-refresh/enrichment
job, so ``GET /library/refresh/{run_id}`` has something to poll.

Same shape as every other repository in this codebase: backed by a shared
:class:`~psycopg_pool.AsyncConnectionPool`, raw parameterized SQL, frozen dataclass results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

DEFAULT_LEASE_SECONDS = 120.0


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

    async def try_begin_delivery(
        self, run_id: str, expected_seq: int, lease_seconds: float = DEFAULT_LEASE_SECONDS
    ) -> bool:
        """Atomically claim a delivery of ``run_id`` at checkpoint ``expected_seq``, transitioning it to
        ``running`` if -- and only if -- that checkpoint is still current.

        This is a compare-and-swap: ``run_id``'s ``seq`` column only advances when
        :meth:`mark_rate_limited` records a real checkpoint, and gets stamped into the payload of the
        continuation message published for that checkpoint (see
        ``curator.jobs.queue_publisher.QueuePublisher.publish_library_refresh_continuation``'s ``seq``
        parameter). A caller uses the return value to distinguish two cases that look identical at the
        message-delivery level but are not: ``True`` means this delivery's checkpoint is still current --
        proceed with processing. ``False`` means this delivery is stale -- either a redelivered copy of a
        message whose checkpoint has already been superseded by a later :meth:`mark_rate_limited` call (a
        newer continuation message for the same run has already been published and is what should be
        processed instead), or the run has already reached a terminal status (``succeeded``/``failed``) and
        has nothing left to do. Either way the caller should settle the message without reprocessing rather
        than restarting the run's whole batch from scratch.

        Replaces the old unconditional ``mark_running`` as ``queue_consumer.py``'s entry point into this
        repository -- ``mark_running`` stomped every run back to ``running`` on every delivery attempt with
        no guard, which is exactly what let a stale redelivery silently erase an already-recorded
        ``rate_limited`` checkpoint before reprocessing started.

        A ``running`` row is additionally only claimable once its lease has lapsed (see
        :meth:`renew_lease`), so a concurrent redelivery cannot start a second copy alongside a live one.

        :param run_id: The run id carried in the delivered message's payload.
        :param expected_seq: The ``seq`` carried in the delivered message's payload (``0`` for a message
            with no ``seq`` field at all, matching this column's default for a freshly created run).
        :param lease_seconds: How long the claim is held before another delivery may take it over, absent
            a :meth:`renew_lease` heartbeat. Must comfortably exceed the heartbeat interval.
        :returns: ``True`` if the claim succeeded and the run is now ``running``; ``False`` if it was
            stale, terminal, or currently leased by a live processor -- nothing was changed.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE job_runs SET status = 'running', error = NULL, updated_at = now(), "
                "lease_expires_at = now() + make_interval(secs => %s) "
                "WHERE run_id = %s AND seq = %s AND status NOT IN ('succeeded', 'failed') "
                "AND (status <> 'running' OR lease_expires_at IS NULL OR lease_expires_at <= now()) "
                "RETURNING run_id",
                (lease_seconds, run_id, expected_seq),
            )
            row = await cur.fetchone()
        return row is not None

    async def renew_lease(self, run_id: str, lease_seconds: float = DEFAULT_LEASE_SECONDS) -> bool:
        """Extend ``run_id``'s processing lease by ``lease_seconds``, only while it is still ``running``.

        :returns: ``True`` if the lease was renewed; ``False`` if the run is no longer ``running``.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE job_runs SET lease_expires_at = now() + make_interval(secs => %s) "
                "WHERE run_id = %s AND status = 'running' RETURNING run_id",
                (lease_seconds, run_id),
            )
            row = await cur.fetchone()
        return row is not None

    async def mark_succeeded(self, run_id: str, result_summary: dict[str, Any] | None = None) -> None:
        """Transition a run to ``succeeded``, optionally recording a JSON summary of what it did.

        :param result_summary: E.g. newly-enriched titles per provider (see
            ``curator.library.library_build_orchestrator.LibraryBuildResult``); ``None`` for runs with
            nothing to summarize (an ``"enrichment"`` admin re-scrape, or a fake in tests).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE job_runs SET status = %s, result_summary = %s, updated_at = now(), "
                "lease_expires_at = NULL WHERE run_id = %s",
                ("succeeded", json.dumps(result_summary) if result_summary is not None else None, run_id),
            )

    async def mark_failed(self, run_id: str, error: str) -> None:
        """Transition a run to ``failed``, recording ``error``."""
        await self._set_status(run_id, "failed", error=error)

    async def mark_rate_limited(self, run_id: str, result_summary: dict[str, Any]) -> int:
        """Transition a run to ``rate_limited``: it stopped early on a provider rate limit and has been
        (or is about to be) republished to resume once the limit lifts.

        Distinct from ``failed`` -- this isn't an error, it's an expected, self-resolving pause -- and from
        ``succeeded`` -- the run isn't actually done yet. ``GET /library/refresh/{run_id}`` surfaces
        ``result_summary`` (titles enriched so far, which provider limited, when it'll resume, how many
        games remain) exactly the same way it already does for a succeeded run.

        Also bumps ``seq``, this run's checkpoint counter, and returns the new value.

        :param result_summary: The same shape :meth:`mark_succeeded` accepts, plus
            ``rate_limited_provider``, ``retry_after_seconds``, and ``remaining_count``.
        :returns: The run's new checkpoint sequence number, to be stamped into the continuation message's
            payload via ``QueuePublisher.publish_library_refresh_continuation``'s ``seq`` parameter -- this
            is the value :meth:`try_begin_delivery` later checks a redelivery's own ``seq`` against.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE job_runs SET status = 'rate_limited', result_summary = %s, seq = seq + 1, "
                "updated_at = now(), lease_expires_at = NULL WHERE run_id = %s RETURNING seq",
                (json.dumps(result_summary), run_id),
            )
            row = await cur.fetchone()
        assert row is not None, f"mark_rate_limited called with unknown run_id {run_id!r}"
        return int(row[0])

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

    async def find_active_run(self, identity_sub: str, kind: str) -> JobRun | None:
        """Return the caller's own most recent non-terminal (``queued``/``running``/``rate_limited``) run
        of this kind, or ``None`` if none exists.

        Backs ``POST /library/refresh``'s duplicate-run guard: a caller with an already-in-flight run
        should get that run's id back, not start a second, genuinely concurrent job against the same real,
        rate-limited PSN/RAWG/OpenCritic APIs. Staleness (deciding whether a returned run is actually still
        alive or should be superseded) is deliberately left to the caller -- this method only reports what
        exists, and :class:`JobRun` carries both ``lease_expires_at`` and ``updated_at`` for that decision.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT run_id, kind, identity_sub, status, error, result_summary, updated_at, lease_expires_at "
                "FROM job_runs WHERE identity_sub = %s AND kind = %s "
                "AND status NOT IN ('succeeded', 'failed') "
                "ORDER BY created_at DESC LIMIT 1",
                (identity_sub, kind),
            )
            row = await cur.fetchone()
        return self._row_to_job_run(row) if row is not None else None

    async def get_latest_by_kind(self, kind: str) -> JobRun | None:
        """Return the most recently *queued* run of this kind, or ``None`` if none exists yet.

        Backs the admin-visible ``GET /enrichment/runs/latest`` status endpoint. Ordered by
        ``created_at`` (queued-time), not ``updated_at`` -- ``updated_at`` bumps on every status
        transition, which would make an old, still-stuck run look "latest" again the moment it's next
        touched (e.g. a stale run superseded by :meth:`find_active_run`'s staleness escape).
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
