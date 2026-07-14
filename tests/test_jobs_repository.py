"""Tests for JobRunsRepository, using hand-written fake async psycopg_pool objects."""

from __future__ import annotations

from curator.jobs.repository import JobRunsRepository


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection

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
    def __init__(self, fetchone_results=None):
        self.executed: list[tuple[str, tuple | None]] = []
        self.fetchone_results = list(fetchone_results or [])

    def cursor(self):
        return FakeCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, fetchone_results=None):
        self._fetchone_results = fetchone_results or []
        self.connections: list[FakeConnection] = []

    def connection(self):
        conn = FakeConnection(fetchone_results=list(self._fetchone_results))
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


async def test_mark_running_updates_status():
    pool = FakePool()
    repo = JobRunsRepository(pool)

    await repo.mark_running("run-1")

    sql, params = pool.connections[0].executed[0]
    assert "UPDATE job_runs" in sql
    assert params == ("running", None, "run-1")


async def test_mark_succeeded_updates_status():
    pool = FakePool()
    repo = JobRunsRepository(pool)

    await repo.mark_succeeded("run-1")

    _sql, params = pool.connections[0].executed[0]
    assert params == ("succeeded", None, "run-1")


async def test_mark_failed_records_error():
    pool = FakePool()
    repo = JobRunsRepository(pool)

    await repo.mark_failed("run-1", "boom")

    _sql, params = pool.connections[0].executed[0]
    assert params == ("failed", "boom", "run-1")


async def test_get_returns_run():
    pool = FakePool(fetchone_results=[("run-1", "library_refresh", "sub-1", "running", None)])
    repo = JobRunsRepository(pool)

    run = await repo.get("run-1")

    assert run is not None
    assert run.run_id == "run-1"
    assert run.kind == "library_refresh"
    assert run.identity_sub == "sub-1"
    assert run.status == "running"
    assert run.error is None


async def test_get_returns_none_when_not_found():
    pool = FakePool(fetchone_results=[None])
    repo = JobRunsRepository(pool)

    run = await repo.get("unknown")

    assert run is None
