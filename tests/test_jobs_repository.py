"""Tests for JobRunsRepository, using hand-written fake async psycopg_pool objects."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from curator.jobs.repository import JobRunsRepository

_NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)


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


async def test_try_begin_delivery_returns_true_and_updates_status_when_row_is_claimed():
    pool = FakePool(fetchone_results=[("run-1",)])
    repo = JobRunsRepository(pool)

    began = await repo.try_begin_delivery("run-1", 0)

    assert began is True
    sql, params = pool.connections[0].executed[0]
    assert "UPDATE job_runs" in sql
    assert "RETURNING run_id" in sql
    assert params == ("run-1", 0)


async def test_try_begin_delivery_returns_false_when_seq_does_not_match():
    """A redelivered message whose payload seq has already been superseded by a later mark_rate_limited
    checkpoint -- the real ``WHERE seq = %s`` clause matches no row, exactly like this fake's configured
    ``None``."""
    pool = FakePool(fetchone_results=[None])
    repo = JobRunsRepository(pool)

    began = await repo.try_begin_delivery("run-1", 3)

    assert began is False
    _sql, params = pool.connections[0].executed[0]
    assert params == ("run-1", 3)


async def test_try_begin_delivery_returns_false_when_status_already_terminal():
    """A run that already reached succeeded/failed -- the real ``AND status NOT IN ('succeeded',
    'failed')`` clause matches no row regardless of seq, exactly like this fake's configured ``None``."""
    pool = FakePool(fetchone_results=[None])
    repo = JobRunsRepository(pool)

    began = await repo.try_begin_delivery("run-1", 0)

    assert began is False


async def test_mark_succeeded_updates_status():
    pool = FakePool()
    repo = JobRunsRepository(pool)

    await repo.mark_succeeded("run-1")

    _sql, params = pool.connections[0].executed[0]
    assert params == ("succeeded", None, "run-1")


async def test_mark_succeeded_serializes_result_summary_as_json():
    pool = FakePool()
    repo = JobRunsRepository(pool)

    await repo.mark_succeeded("run-1", {"rawg_enriched_titles": ["Elden Ring"]})

    _sql, params = pool.connections[0].executed[0]
    assert params is not None
    assert params[0] == "succeeded"
    assert params[1] == '{"rawg_enriched_titles": ["Elden Ring"]}'
    assert params[2] == "run-1"


async def test_mark_failed_records_error():
    pool = FakePool()
    repo = JobRunsRepository(pool)

    await repo.mark_failed("run-1", "boom")

    _sql, params = pool.connections[0].executed[0]
    assert params == ("failed", "boom", "run-1")


async def test_mark_rate_limited_serializes_result_summary_sets_status_and_returns_new_seq():
    pool = FakePool(fetchone_results=[(1,)])
    repo = JobRunsRepository(pool)
    summary = {
        "rawg_enriched_titles": ["Elden Ring"],
        "opencritic_enriched_titles": [],
        "opencritic_topup_incomplete": False,
        "rate_limited_provider": "rawg",
        "retry_after_seconds": 3600.0,
        "remaining_count": 4,
    }

    new_seq = await repo.mark_rate_limited("run-1", summary)

    assert new_seq == 1
    sql, params = pool.connections[0].executed[0]
    assert "UPDATE job_runs" in sql
    assert "RETURNING seq" in sql
    assert params is not None
    assert json.loads(params[0]) == summary
    assert params[1] == "run-1"


async def test_mark_rate_limited_returns_an_incrementing_seq_across_repeated_calls():
    """Each call opens its own connection (a fresh ``UPDATE ... RETURNING seq`` against real Postgres),
    so the fake's per-call return value is set between calls rather than queued up front -- see
    ``FakePool.connection()``, which hands every new connection a fresh copy of the pool's configured
    ``fetchone_results``."""
    pool = FakePool(fetchone_results=[(1,)])
    repo = JobRunsRepository(pool)
    summary = {"rawg_enriched_titles": [], "opencritic_enriched_titles": [], "opencritic_topup_incomplete": False}

    first_seq = await repo.mark_rate_limited("run-1", summary)
    pool._fetchone_results = [(2,)]
    second_seq = await repo.mark_rate_limited("run-1", summary)

    assert first_seq == 1
    assert second_seq == 2
    assert second_seq == first_seq + 1


async def test_get_returns_run():
    pool = FakePool(fetchone_results=[("run-1", "library_refresh", "sub-1", "running", None, None, _NOW)])
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


async def test_get_returns_result_summary():
    summary = {"rawg_enriched_titles": ["Elden Ring"], "opencritic_topup_incomplete": True}
    pool = FakePool(fetchone_results=[("run-1", "library_refresh", "sub-1", "succeeded", None, summary, _NOW)])
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
    pool = FakePool(fetchone_results=[("run-1", "library_refresh", "sub-1", "running", None, None, _NOW)])
    repo = JobRunsRepository(pool)

    run = await repo.find_active_run("sub-1", "library_refresh")

    assert run is not None
    assert run.run_id == "run-1"
    sql, params = pool.connections[0].executed[0]
    assert "identity_sub = %s AND kind = %s" in sql
    assert "status NOT IN ('succeeded', 'failed')" in sql
    assert "ORDER BY created_at DESC LIMIT 1" in sql
    assert params == ("sub-1", "library_refresh")


async def test_find_active_run_returns_none_when_no_non_terminal_row_exists():
    pool = FakePool(fetchone_results=[None])
    repo = JobRunsRepository(pool)

    run = await repo.find_active_run("sub-1", "library_refresh")

    assert run is None


async def test_get_latest_by_kind_returns_matching_row():
    pool = FakePool(fetchone_results=[("run-1", "enrichment", None, "succeeded", None, None, _NOW)])
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
