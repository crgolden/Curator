"""Tests for Repository, using hand-written fake async psycopg_pool/connection/cursor objects (no real
database, no unittest.mock)."""

from __future__ import annotations

from datetime import datetime, timezone

from curator.persistence.repository import LinkRecord, Repository


class FakeCursor:
    """Stands in for an async psycopg cursor: records executed SQL/params, returns a queued fetchone() row."""

    def __init__(self, connection: FakeConnection) -> None:
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
    """Stands in for an async psycopg connection checked out from a pool: hands out FakeCursors."""

    def __init__(self, fetchone_result=None) -> None:
        self.executed: list[tuple[str, tuple | None]] = []
        self.fetchone_result = fetchone_result

    def cursor(self):
        return FakeCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    """Stands in for psycopg_pool.AsyncConnectionPool: hands out (and remembers) a fresh FakeConnection
    per ``connection()`` call."""

    def __init__(self, fetchone_result=None) -> None:
        self._fetchone_result = fetchone_result
        self.connections: list[FakeConnection] = []

    def connection(self) -> FakeConnection:
        conn = FakeConnection(fetchone_result=self._fetchone_result)
        self.connections.append(conn)
        return conn


async def test_upsert_user_executes_upsert():
    pool = FakePool()
    repo = Repository(pool)

    await repo.upsert_user("11111111-1111-1111-1111-111111111111")

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "INSERT INTO app_users" in sql
    assert "ON CONFLICT (identity_sub) DO UPDATE SET updated_at = now()" in sql
    assert params == ("11111111-1111-1111-1111-111111111111",)


async def test_touch_login_executes_update():
    pool = FakePool()
    repo = Repository(pool)

    await repo.touch_login("sub-1")

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "UPDATE app_users" in sql
    assert "last_login_at = now()" in sql
    assert params == ("sub-1",)


async def test_get_link_returns_none_when_no_row():
    pool = FakePool(fetchone_result=None)
    repo = Repository(pool)

    assert await repo.get_link("sub-1") is None


async def test_get_link_maps_row_to_link_record():
    linked_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    updated_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    access_expires = datetime(2026, 1, 2, tzinfo=timezone.utc)
    refresh_expires = datetime(2026, 3, 1, tzinfo=timezone.utc)
    last_verified_at = datetime(2026, 2, 2, tzinfo=timezone.utc)
    row = (
        "psn-account-1",
        b"encrypted-bytes",
        access_expires,
        refresh_expires,
        linked_at,
        updated_at,
        last_verified_at,
    )
    pool = FakePool(fetchone_result=row)
    repo = Repository(pool)

    result = await repo.get_link("sub-1")

    assert result == LinkRecord(
        psn_account_id="psn-account-1",
        token_response_enc=b"encrypted-bytes",
        access_token_expires_at=access_expires,
        refresh_token_expires_at=refresh_expires,
        linked_at=linked_at,
        updated_at=updated_at,
        last_verified_at=last_verified_at,
    )
    sql, params = pool.connections[0].executed[0]
    assert "SELECT" in sql
    assert "FROM psn_links WHERE identity_sub = %s" in sql
    assert params == ("sub-1",)


async def test_touch_link_verified_executes_update():
    pool = FakePool()
    repo = Repository(pool)

    await repo.touch_link_verified("sub-1")

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "UPDATE psn_links SET last_verified_at = now()" in sql
    assert params == ("sub-1",)


async def test_upsert_link_sql_has_on_conflict_coalesce_and_correct_params():
    pool = FakePool()
    repo = Repository(pool)
    access_expires = datetime(2026, 1, 2, tzinfo=timezone.utc)
    refresh_expires = datetime(2026, 3, 1, tzinfo=timezone.utc)

    await repo.upsert_link(
        "sub-1",
        b"encrypted",
        access_expires,
        refresh_expires,
        psn_account_id="psn-account-1",
    )

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "INSERT INTO psn_links" in sql
    assert "ON CONFLICT (identity_sub) DO UPDATE SET" in sql
    assert "COALESCE(EXCLUDED.psn_account_id, psn_links.psn_account_id)" in sql
    assert "updated_at = now()" in sql
    assert params == ("sub-1", "psn-account-1", b"encrypted", access_expires, refresh_expires)


async def test_upsert_link_defaults_psn_account_id_to_none():
    pool = FakePool()
    repo = Repository(pool)

    await repo.upsert_link("sub-1", b"encrypted", None, None)

    _, params = pool.connections[0].executed[0]
    assert params == ("sub-1", None, b"encrypted", None, None)


async def test_set_link_account_executes_update():
    pool = FakePool()
    repo = Repository(pool)

    await repo.set_link_account("sub-1", "psn-account-1")

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "UPDATE psn_links SET psn_account_id" in sql
    assert params == ("psn-account-1", "sub-1")


async def test_delete_link_executes_delete():
    pool = FakePool()
    repo = Repository(pool)

    await repo.delete_link("sub-1")

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "DELETE FROM psn_links WHERE identity_sub = %s" in sql
    assert params == ("sub-1",)


async def test_each_method_call_checks_out_its_own_connection():
    pool = FakePool()
    repo = Repository(pool)

    await repo.upsert_user("sub-1")
    await repo.touch_login("sub-1")

    assert len(pool.connections) == 2
