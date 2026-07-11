"""Tests for Repository, using hand-written fake psycopg connection/cursor objects (no real database,
no unittest.mock)."""

from __future__ import annotations

from datetime import datetime, timezone

from curator.persistence.repository import LinkRecord, Repository


class FakeCursor:
    """Stands in for a psycopg.Cursor: records executed SQL/params, returns a queued fetchone() row."""

    def __init__(self, connection: "FakeConnection") -> None:
        self._connection = connection

    def execute(self, sql, params=None):
        self._connection.executed.append((sql, params))

    def fetchone(self):
        return self._connection.fetchone_result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    """Stands in for a psycopg.Connection: tracks commit calls and hands out FakeCursors."""

    def __init__(self, fetchone_result=None) -> None:
        self.executed: list[tuple[str, tuple | None]] = []
        self.committed = False
        self.closed = False
        self.fetchone_result = fetchone_result

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.closed = True
        return False


class FakeConnectionFactory:
    """A connection_factory that hands out (and remembers) a fresh FakeConnection per call."""

    def __init__(self, fetchone_result=None) -> None:
        self._fetchone_result = fetchone_result
        self.connections: list[FakeConnection] = []

    def __call__(self) -> FakeConnection:
        conn = FakeConnection(fetchone_result=self._fetchone_result)
        self.connections.append(conn)
        return conn


def test_upsert_user_executes_upsert_and_commits():
    factory = FakeConnectionFactory()
    repo = Repository(factory)

    repo.upsert_user("11111111-1111-1111-1111-111111111111")

    conn = factory.connections[0]
    sql, params = conn.executed[0]
    assert "INSERT INTO app_users" in sql
    assert "ON CONFLICT (identity_sub) DO UPDATE SET updated_at = now()" in sql
    assert params == ("11111111-1111-1111-1111-111111111111",)
    assert conn.committed


def test_touch_login_executes_update_and_commits():
    factory = FakeConnectionFactory()
    repo = Repository(factory)

    repo.touch_login("sub-1")

    conn = factory.connections[0]
    sql, params = conn.executed[0]
    assert "UPDATE app_users" in sql
    assert "last_login_at = now()" in sql
    assert params == ("sub-1",)
    assert conn.committed


def test_get_link_returns_none_when_no_row():
    factory = FakeConnectionFactory(fetchone_result=None)
    repo = Repository(factory)

    assert repo.get_link("sub-1") is None
    assert factory.connections[0].committed


def test_get_link_maps_row_to_link_record():
    linked_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    updated_at = datetime(2026, 2, 1, tzinfo=timezone.utc)
    access_expires = datetime(2026, 1, 2, tzinfo=timezone.utc)
    refresh_expires = datetime(2026, 3, 1, tzinfo=timezone.utc)
    last_verified_at = datetime(2026, 2, 2, tzinfo=timezone.utc)
    row = (
        "psn-account-1", b"encrypted-bytes", access_expires, refresh_expires, linked_at, updated_at,
        last_verified_at,
    )
    factory = FakeConnectionFactory(fetchone_result=row)
    repo = Repository(factory)

    result = repo.get_link("sub-1")

    assert result == LinkRecord(
        psn_account_id="psn-account-1",
        token_response_enc=b"encrypted-bytes",
        access_token_expires_at=access_expires,
        refresh_token_expires_at=refresh_expires,
        linked_at=linked_at,
        updated_at=updated_at,
        last_verified_at=last_verified_at,
    )
    sql, params = factory.connections[0].executed[0]
    assert "SELECT" in sql
    assert "FROM psn_links WHERE identity_sub = %s" in sql
    assert params == ("sub-1",)


def test_touch_link_verified_executes_update_and_commits():
    factory = FakeConnectionFactory()
    repo = Repository(factory)

    repo.touch_link_verified("sub-1")

    conn = factory.connections[0]
    sql, params = conn.executed[0]
    assert "UPDATE psn_links SET last_verified_at = now()" in sql
    assert params == ("sub-1",)
    assert conn.committed


def test_upsert_link_sql_has_on_conflict_coalesce_and_correct_params():
    factory = FakeConnectionFactory()
    repo = Repository(factory)
    access_expires = datetime(2026, 1, 2, tzinfo=timezone.utc)
    refresh_expires = datetime(2026, 3, 1, tzinfo=timezone.utc)

    repo.upsert_link(
        "sub-1",
        b"encrypted",
        access_expires,
        refresh_expires,
        psn_account_id="psn-account-1",
    )

    conn = factory.connections[0]
    sql, params = conn.executed[0]
    assert "INSERT INTO psn_links" in sql
    assert "ON CONFLICT (identity_sub) DO UPDATE SET" in sql
    assert "COALESCE(EXCLUDED.psn_account_id, psn_links.psn_account_id)" in sql
    assert "updated_at = now()" in sql
    assert params == ("sub-1", "psn-account-1", b"encrypted", access_expires, refresh_expires)
    assert conn.committed


def test_upsert_link_defaults_psn_account_id_to_none():
    factory = FakeConnectionFactory()
    repo = Repository(factory)

    repo.upsert_link("sub-1", b"encrypted", None, None)

    _, params = factory.connections[0].executed[0]
    assert params == ("sub-1", None, b"encrypted", None, None)


def test_set_link_account_executes_update_and_commits():
    factory = FakeConnectionFactory()
    repo = Repository(factory)

    repo.set_link_account("sub-1", "psn-account-1")

    conn = factory.connections[0]
    sql, params = conn.executed[0]
    assert "UPDATE psn_links SET psn_account_id" in sql
    assert params == ("psn-account-1", "sub-1")
    assert conn.committed


def test_delete_link_executes_delete_and_commits():
    factory = FakeConnectionFactory()
    repo = Repository(factory)

    repo.delete_link("sub-1")

    conn = factory.connections[0]
    sql, params = conn.executed[0]
    assert "DELETE FROM psn_links WHERE identity_sub = %s" in sql
    assert params == ("sub-1",)
    assert conn.committed


def test_each_method_call_opens_its_own_connection():
    factory = FakeConnectionFactory()
    repo = Repository(factory)

    repo.upsert_user("sub-1")
    repo.touch_login("sub-1")

    assert len(factory.connections) == 2
