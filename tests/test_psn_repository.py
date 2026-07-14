"""Tests for curator.psn.repository.TestAccountRepository, using hand-written fake async psycopg_pool
objects (no real database, no unittest.mock) -- same pattern as tests/test_repository.py."""

from __future__ import annotations

from curator.psn.repository import TestAccountRepository


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection

    async def execute(self, sql, params=None):
        self._connection.executed.append((sql, params))

    async def fetchone(self):
        return self._connection.fetchone_result

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, fetchone_result=None):
        self.executed: list[tuple[str, tuple | None]] = []
        self.fetchone_result = fetchone_result

    def cursor(self):
        return FakeCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, fetchone_result=None):
        self._fetchone_result = fetchone_result
        self.connections: list[FakeConnection] = []

    def connection(self):
        conn = FakeConnection(fetchone_result=self._fetchone_result)
        self.connections.append(conn)
        return conn


async def test_get_pinned_account_id_returns_none_when_no_row():
    repo = TestAccountRepository(FakePool(fetchone_result=None))

    assert await repo.get_pinned_account_id("sub-1") is None


async def test_get_pinned_account_id_returns_the_pinned_id():
    pool = FakePool(fetchone_result=("psn-account-1",))
    repo = TestAccountRepository(pool)

    result = await repo.get_pinned_account_id("sub-1")

    assert result == "psn-account-1"
    sql, params = pool.connections[0].executed[0]
    assert "SELECT psn_account_id FROM psn_test_accounts WHERE identity_sub = %s" in sql
    assert params == ("sub-1",)


async def test_pin_upserts_with_on_conflict():
    pool = FakePool()
    repo = TestAccountRepository(pool)

    await repo.pin("sub-1", "psn-account-1")

    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO psn_test_accounts" in sql
    assert "ON CONFLICT (identity_sub) DO UPDATE SET" in sql
    assert params == ("sub-1", "psn-account-1")
