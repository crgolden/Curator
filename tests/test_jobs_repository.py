"""Tests for JobRunsRepository, using hand-written fake async psycopg_pool objects."""

from __future__ import annotations

from datetime import datetime, timezone

from curator.jobs.repository import TERMINAL_STATUSES, JobRunsRepository

_NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
_LEASE = datetime(2026, 8, 3, 12, 2, 0, tzinfo=timezone.utc)


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection
        self.rowcount = connection.rowcount

    async def execute(self, sql, params=None):
        self._connection.executed.append((sql, params))

    async def fetchone(self):
        if self._connection.fetchone_results:
            return self._connection.fetchone_results.pop(0)
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, fetchone_results=None, rowcount=0):
        self.executed: list[tuple[str, tuple | None]] = []
        self.fetchone_results = list(fetchone_results or [])
        self.rowcount = rowcount

    def cursor(self):
        return FakeCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, fetchone_results=None, rowcount=0):
        self._fetchone_results = fetchone_results or []
        self._rowcount = rowcount
        self.connections: list[FakeConnection] = []

    def connection(self):
        conn = FakeConnection(fetchone_results=list(self._fetchone_results), rowcount=self._rowcount)
        self.connections.append(conn)
        return conn


async def test_create_inserts_queued_row():
    pool = FakePool()
    repo = JobRunsRepository(pool)

    await repo.create("run-1", "library_refresh", "sub-1")

    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO job_runs" in sql
    assert params == ("run-1", "library_refresh", "sub-1")


async def test_create_defaults_identity_sub_to_none():
    pool = FakePool()
    repo = JobRunsRepository(pool)

    await repo.create("run-1", "enrichment")

    _sql, params = pool.connections[0].executed[0]
    assert params == ("run-1", "enrichment", None)


async def test_mark_failed_clears_the_lease_and_records_error():
    """Every exit from 'running' must release the lease, or a finished run would stay unclaimable for the
    rest of its lease window."""
    pool = FakePool()
    repo = JobRunsRepository(pool)

    await repo.mark_failed("run-1", "boom")

    sql, params = pool.connections[0].executed[0]
    assert "lease_expires_at = NULL" in sql
    assert params == ("failed", "boom", "run-1")


async def test_get_returns_run():
    pool = FakePool(fetchone_results=[("run-1", "library_refresh", "sub-1", "running", None, None, _NOW, _LEASE)])
    repo = JobRunsRepository(pool)

    run = await repo.get("run-1")

    assert run is not None
    assert run.run_id == "run-1"
    assert run.kind == "library_refresh"
    assert run.identity_sub == "sub-1"
    assert run.status == "running"
    assert run.error is None
    assert run.result_summary is None
    assert run.updated_at == _NOW
    assert run.lease_expires_at == _LEASE


async def test_get_returns_result_summary():
    summary = {"rawg_enriched_titles": ["Elden Ring"], "opencritic_topup_incomplete": True}
    pool = FakePool(fetchone_results=[("run-1", "library_refresh", "sub-1", "succeeded", None, summary, _NOW, None)])
    repo = JobRunsRepository(pool)

    run = await repo.get("run-1")

    assert run is not None
    assert run.result_summary == summary


async def test_get_returns_none_when_not_found():
    pool = FakePool(fetchone_results=[None])
    repo = JobRunsRepository(pool)

    run = await repo.get("unknown")

    assert run is None


async def test_find_active_run_returns_matching_row():
    pool = FakePool(fetchone_results=[("run-1", "library_refresh", "sub-1", "running", None, None, _NOW, _LEASE)])
    repo = JobRunsRepository(pool)

    run = await repo.find_active_run("sub-1", "library_refresh")

    assert run is not None
    assert run.run_id == "run-1"
    sql, params = pool.connections[0].executed[0]
    assert "identity_sub = %s AND kind = %s" in sql
    assert "status <> ALL(%s)" in sql
    assert "ORDER BY created_at DESC LIMIT 1" in sql
    assert params == ("sub-1", "library_refresh", ["succeeded", "failed", "cancelled"])


async def test_find_active_run_returns_none_when_no_non_terminal_row_exists():
    pool = FakePool(fetchone_results=[None])
    repo = JobRunsRepository(pool)

    run = await repo.find_active_run("sub-1", "library_refresh")

    assert run is None


async def test_terminal_statuses_covers_cancelled():
    """0046 added a THIRD terminal status. Every 'is this run still active' predicate is spelled as the
    complement of this tuple, so a value missing here leaves a cancelled run blocking new runs forever --
    which is worse than the stuck run cancellation exists to clear."""
    assert TERMINAL_STATUSES == ("succeeded", "failed", "cancelled")


async def test_find_active_global_run_excludes_every_terminal_status():
    pool = FakePool(fetchone_results=[None])
    repo = JobRunsRepository(pool)

    await repo.find_active_global_run("enrichment")

    sql, params = pool.connections[0].executed[0]
    assert "identity_sub IS NULL AND kind = %s" in sql
    assert "status <> ALL(%s)" in sql
    assert params == ("enrichment", list(TERMINAL_STATUSES))


async def test_cancel_keeps_the_row_clears_the_lease_and_records_the_reason():
    pool = FakePool(rowcount=1)
    repo = JobRunsRepository(pool)

    assert await repo.cancel("run-1", "stood down") is True

    sql, params = pool.connections[0].executed[0]
    assert sql.strip().startswith("UPDATE job_runs SET status = 'cancelled'"), (
        "job_runs is the audit trail the reaper and every operator query read, so a stuck run is stood "
        "down rather than deleted"
    )
    assert "lease_expires_at = NULL" in sql
    assert "status <> ALL(%s)" in sql
    assert params == ("stood down", "run-1", list(TERMINAL_STATUSES))


async def test_cancel_reports_false_when_the_run_had_already_finished():
    pool = FakePool(rowcount=0)
    repo = JobRunsRepository(pool)

    assert await repo.cancel("run-1", "stood down") is False, (
        "the non-terminal guard is what stops a late cancel rewriting a succeeded run's outcome"
    )


async def test_get_latest_by_kind_returns_matching_row():
    pool = FakePool(fetchone_results=[("run-1", "enrichment", None, "succeeded", None, None, _NOW, None)])
    repo = JobRunsRepository(pool)

    run = await repo.get_latest_by_kind("enrichment")

    assert run is not None
    assert run.run_id == "run-1"
    sql, params = pool.connections[0].executed[0]
    assert "kind = %s" in sql
    assert "ORDER BY created_at DESC LIMIT 1" in sql
    assert params == ("enrichment",)


async def test_get_latest_by_kind_returns_none_when_no_run_of_that_kind_exists():
    pool = FakePool(fetchone_results=[None])
    repo = JobRunsRepository(pool)

    run = await repo.get_latest_by_kind("enrichment")

    assert run is None
