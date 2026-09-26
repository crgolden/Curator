"""Tests for curator.psn.repository.PinnedAccountRepository, using hand-written fake async psycopg_pool
objects (no real database, no unittest.mock) -- same pattern as tests/test_repository.py."""

from __future__ import annotations

from curator.psn.repository import GET_PINNED_ACCOUNT_SQL, PIN_ACCOUNT_SQL, PinnedAccountRepository
from test_values import new_account_id, new_identity_sub


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
    repo = PinnedAccountRepository(FakePool(fetchone_result=None))

    assert await repo.get_pinned_account_id(new_identity_sub()) is None


async def test_get_pinned_account_id_returns_the_pinned_id():
    identity_sub, account_id = new_identity_sub(), new_account_id()
    pool = FakePool(fetchone_result=(account_id,))

    result = await PinnedAccountRepository(pool).get_pinned_account_id(identity_sub)

    assert result == account_id
    assert pool.connections[0].executed == [(GET_PINNED_ACCOUNT_SQL, (identity_sub,))]


async def test_pin_runs_the_upsert_with_the_user_and_account():
    identity_sub, account_id = new_identity_sub(), new_account_id()
    pool = FakePool()

    await PinnedAccountRepository(pool).pin(identity_sub, account_id)

    assert pool.connections[0].executed == [(PIN_ACCOUNT_SQL, (identity_sub, account_id))]
