"""Tests for JobRunsRepository, using hand-written fake async psycopg_pool objects."""

from __future__ import annotations

from datetime import timedelta

from curator.jobs.repository import (
    JOB_KIND_ENRICHMENT,
    JOB_KIND_LIBRARY_REFRESH,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    NEWEST_FIRST_SQL,
    NOT_TERMINAL_SQL,
    TERMINAL_STATUSES,
    JobRun,
    JobRunsRepository,
)
from test_values import (
    lowercase_token,
    new_identity_sub,
    new_positive_count,
    new_result_summary,
    new_run_id,
    new_utc_instant,
)


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


def _row(run: JobRun) -> tuple[object, ...]:
    return (
        run.run_id,
        run.kind,
        run.identity_sub,
        run.status,
        run.error,
        run.result_summary,
        run.updated_at,
        run.lease_expires_at,
    )


def _running_library_refresh(identity_sub: str) -> JobRun:
    updated_at = new_utc_instant()
    return JobRun(
        run_id=new_run_id(),
        kind=JOB_KIND_LIBRARY_REFRESH,
        identity_sub=identity_sub,
        status=JOB_STATUS_RUNNING,
        error=None,
        result_summary=None,
        updated_at=updated_at,
        lease_expires_at=updated_at + timedelta(seconds=new_positive_count()),
    )


async def test_create_inserts_queued_row():
    pool = FakePool()
    repo = JobRunsRepository(pool)
    run_id = new_run_id()
    identity_sub = new_identity_sub()

    await repo.create(run_id, JOB_KIND_LIBRARY_REFRESH, identity_sub)

    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO job_runs" in sql
    assert params == (run_id, JOB_KIND_LIBRARY_REFRESH, identity_sub)


async def test_create_defaults_identity_sub_to_none():
    pool = FakePool()
    repo = JobRunsRepository(pool)
    run_id = new_run_id()

    await repo.create(run_id, JOB_KIND_ENRICHMENT)

    _sql, params = pool.connections[0].executed[0]
    assert params == (run_id, JOB_KIND_ENRICHMENT, None)


async def test_mark_failed_clears_the_lease_and_records_error():
    """Every exit from 'running' must release the lease, or a finished run would stay unclaimable for the
    rest of its lease window."""
    pool = FakePool()
    repo = JobRunsRepository(pool)
    run_id = new_run_id()
    error = lowercase_token()

    await repo.mark_failed(run_id, error)

    sql, params = pool.connections[0].executed[0]
    assert "lease_expires_at = NULL" in sql
    assert params == (JOB_STATUS_FAILED, error, run_id)


async def test_get_returns_run():
    stored = _running_library_refresh(new_identity_sub())
    pool = FakePool(fetchone_results=[_row(stored)])
    repo = JobRunsRepository(pool)

    run = await repo.get(stored.run_id)

    assert run == stored


async def test_get_returns_result_summary():
    summary = new_result_summary()
    stored = JobRun(
        run_id=new_run_id(),
        kind=JOB_KIND_LIBRARY_REFRESH,
        identity_sub=new_identity_sub(),
        status=JOB_STATUS_SUCCEEDED,
        error=None,
        result_summary=summary,
        updated_at=new_utc_instant(),
    )
    pool = FakePool(fetchone_results=[_row(stored)])
    repo = JobRunsRepository(pool)

    run = await repo.get(stored.run_id)

    assert run is not None
    assert run.result_summary == summary


async def test_get_returns_none_when_not_found():
    pool = FakePool(fetchone_results=[None])
    repo = JobRunsRepository(pool)

    run = await repo.get(new_run_id())

    assert run is None


async def test_find_active_run_returns_matching_row():
    identity_sub = new_identity_sub()
    stored = _running_library_refresh(identity_sub)
    pool = FakePool(fetchone_results=[_row(stored)])
    repo = JobRunsRepository(pool)

    run = await repo.find_active_run(identity_sub, JOB_KIND_LIBRARY_REFRESH)

    assert run == stored
    sql, params = pool.connections[0].executed[0]
    assert "identity_sub = %s AND kind = %s" in sql
    assert NOT_TERMINAL_SQL in sql
    assert NEWEST_FIRST_SQL in sql
    assert params == (identity_sub, JOB_KIND_LIBRARY_REFRESH, list(TERMINAL_STATUSES))


async def test_find_active_run_returns_none_when_no_non_terminal_row_exists():
    pool = FakePool(fetchone_results=[None])
    repo = JobRunsRepository(pool)

    run = await repo.find_active_run(new_identity_sub(), JOB_KIND_LIBRARY_REFRESH)

    assert run is None


async def test_terminal_statuses_covers_cancelled():
    """0046 added a THIRD terminal status. Every 'is this run still active' predicate is spelled as the
    complement of this tuple, so a value missing here leaves a cancelled run blocking new runs forever --
    which is worse than the stuck run cancellation exists to clear."""
    assert TERMINAL_STATUSES == ("succeeded", "failed", "cancelled")


async def test_find_active_global_run_excludes_every_terminal_status():
    pool = FakePool(fetchone_results=[None])
    repo = JobRunsRepository(pool)

    await repo.find_active_global_run(JOB_KIND_ENRICHMENT)

    sql, params = pool.connections[0].executed[0]
    assert "identity_sub IS NULL AND kind = %s" in sql
    assert NOT_TERMINAL_SQL in sql
    assert NEWEST_FIRST_SQL in sql
    assert params == (JOB_KIND_ENRICHMENT, list(TERMINAL_STATUSES))


async def test_cancel_keeps_the_row_clears_the_lease_and_records_the_reason():
    pool = FakePool(rowcount=1)
    repo = JobRunsRepository(pool)
    run_id = new_run_id()
    reason = lowercase_token()

    cancelled = await repo.cancel(run_id, reason)

    assert cancelled is True
    sql, params = pool.connections[0].executed[0]
    assert sql.strip().startswith("UPDATE job_runs SET status = 'cancelled'"), (
        "job_runs is the audit trail the reaper and every operator query read, so a stuck run is stood "
        "down rather than deleted"
    )
    assert "lease_expires_at = NULL" in sql
    assert NOT_TERMINAL_SQL in sql
    assert params == (reason, run_id, list(TERMINAL_STATUSES))


async def test_cancel_reports_false_when_the_run_had_already_finished():
    pool = FakePool(rowcount=0)
    repo = JobRunsRepository(pool)

    cancelled = await repo.cancel(new_run_id(), lowercase_token())

    assert cancelled is False, "the non-terminal guard is what stops a late cancel rewriting a succeeded run's outcome"


async def test_get_latest_by_kind_returns_matching_row():
    stored = JobRun(
        run_id=new_run_id(),
        kind=JOB_KIND_ENRICHMENT,
        identity_sub=None,
        status=JOB_STATUS_SUCCEEDED,
        error=None,
        result_summary=None,
        updated_at=new_utc_instant(),
    )
    pool = FakePool(fetchone_results=[_row(stored)])
    repo = JobRunsRepository(pool)

    run = await repo.get_latest_by_kind(JOB_KIND_ENRICHMENT)

    assert run == stored
    sql, params = pool.connections[0].executed[0]
    assert "kind = %s" in sql
    assert NEWEST_FIRST_SQL in sql
    assert params == (JOB_KIND_ENRICHMENT,)


async def test_get_latest_by_kind_returns_none_when_no_run_of_that_kind_exists():
    pool = FakePool(fetchone_results=[None])
    repo = JobRunsRepository(pool)

    run = await repo.get_latest_by_kind(JOB_KIND_ENRICHMENT)

    assert run is None
