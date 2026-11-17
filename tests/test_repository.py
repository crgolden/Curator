"""Tests for Repository, using hand-written fake async psycopg_pool/connection/cursor objects (no real
database, no unittest.mock)."""

from __future__ import annotations

from curator.persistence.repository import LinkRecord, Repository
from test_values import new_account_id, new_identity_sub, new_opaque_token, new_utc_instant


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
    sub = new_identity_sub()
    pool = FakePool()
    repo = Repository(pool)

    await repo.upsert_user(sub)

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "INSERT INTO app_users" in sql
    assert "ON CONFLICT (identity_sub) DO UPDATE SET updated_at = now()" in sql
    assert params == (sub,)


async def test_touch_login_executes_update():
    sub = new_identity_sub()
    pool = FakePool()
    repo = Repository(pool)

    await repo.touch_login(sub)

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "UPDATE app_users" in sql
    assert "last_login_at = now()" in sql
    assert params == (sub,)


async def test_get_link_returns_none_when_no_row():
    pool = FakePool(fetchone_result=None)
    repo = Repository(pool)

    assert await repo.get_link(new_identity_sub()) is None


async def test_get_link_maps_row_to_link_record():
    sub = new_identity_sub()
    psn_account_id = new_account_id()
    token_response_enc = new_opaque_token().encode()
    linked_at = new_utc_instant()
    updated_at = new_utc_instant()
    access_expires = new_utc_instant()
    refresh_expires = new_utc_instant()
    last_verified_at = new_utc_instant()
    row = (
        psn_account_id,
        token_response_enc,
        access_expires,
        refresh_expires,
        linked_at,
        updated_at,
        last_verified_at,
        False,
        False,
        False,
        False,
        False,
        False,
    )
    pool = FakePool(fetchone_result=row)
    repo = Repository(pool)

    result = await repo.get_link(sub)

    assert result == LinkRecord(
        psn_account_id=psn_account_id,
        token_response_enc=token_response_enc,
        access_token_expires_at=access_expires,
        refresh_token_expires_at=refresh_expires,
        linked_at=linked_at,
        updated_at=updated_at,
        last_verified_at=last_verified_at,
        harvest_trophies=False,
        harvest_identity=False,
        harvest_presence=False,
        harvest_devices=False,
        allow_friend_writes=False,
        allow_chat_writes=False,
    )
    sql, params = pool.connections[0].executed[0]
    assert "SELECT" in sql
    assert "harvest_trophies, harvest_identity, harvest_presence, harvest_devices" in sql
    assert "allow_friend_writes, allow_chat_writes" in sql
    assert "FROM psn_links WHERE identity_sub = %s" in sql
    assert params == (sub,)


async def test_get_link_maps_harvest_flags_when_some_are_true():
    row = (
        new_account_id(),
        new_opaque_token().encode(),
        None,
        None,
        new_utc_instant(),
        new_utc_instant(),
        None,
        True,
        False,
        True,
        False,
        False,
        True,
    )
    pool = FakePool(fetchone_result=row)
    repo = Repository(pool)

    result = await repo.get_link(new_identity_sub())

    assert result is not None
    assert result.harvest_trophies is True
    assert result.harvest_identity is False
    assert result.harvest_presence is True
    assert result.harvest_devices is False
    assert result.allow_friend_writes is False
    assert result.allow_chat_writes is True


async def test_touch_link_verified_executes_update():
    sub = new_identity_sub()
    pool = FakePool()
    repo = Repository(pool)

    await repo.touch_link_verified(sub)

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "UPDATE psn_links SET last_verified_at = now()" in sql
    assert params == (sub,)


async def test_upsert_link_sql_has_on_conflict_coalesce_and_correct_params():
    sub = new_identity_sub()
    psn_account_id = new_account_id()
    token_response_enc = new_opaque_token().encode()
    pool = FakePool()
    repo = Repository(pool)
    access_expires = new_utc_instant()
    refresh_expires = new_utc_instant()

    await repo.upsert_link(
        sub,
        token_response_enc,
        access_expires,
        refresh_expires,
        psn_account_id=psn_account_id,
    )

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "INSERT INTO psn_links" in sql
    assert "ON CONFLICT (identity_sub) DO UPDATE SET" in sql
    assert "COALESCE(EXCLUDED.psn_account_id, psn_links.psn_account_id)" in sql
    assert "updated_at = now()" in sql
    assert params == (sub, psn_account_id, token_response_enc, access_expires, refresh_expires)


async def test_upsert_link_defaults_psn_account_id_to_none():
    sub = new_identity_sub()
    token_response_enc = new_opaque_token().encode()
    pool = FakePool()
    repo = Repository(pool)

    await repo.upsert_link(sub, token_response_enc, None, None)

    _, params = pool.connections[0].executed[0]
    assert params == (sub, None, token_response_enc, None, None)


async def test_set_link_account_executes_update():
    sub = new_identity_sub()
    psn_account_id = new_account_id()
    pool = FakePool()
    repo = Repository(pool)

    await repo.set_link_account(sub, psn_account_id)

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "UPDATE psn_links SET psn_account_id" in sql
    assert params == (psn_account_id, sub)


async def test_set_psn_preferences_executes_update_with_every_capability_flag():
    sub = new_identity_sub()
    pool = FakePool()
    repo = Repository(pool)

    await repo.set_psn_preferences(
        sub,
        harvest_trophies=True,
        harvest_identity=False,
        harvest_presence=True,
        harvest_devices=False,
        allow_friend_writes=True,
        allow_chat_writes=False,
    )

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "UPDATE psn_links SET harvest_trophies = %s, harvest_identity = %s" in sql
    assert "harvest_presence = %s, harvest_devices = %s, allow_friend_writes = %s" in sql
    assert "allow_chat_writes = %s, updated_at = now() WHERE identity_sub = %s" in sql
    assert params == (True, False, True, False, True, False, sub)


async def test_set_psn_preferences_defaults_write_flags_off_when_caller_omits_them():
    sub = new_identity_sub()
    pool = FakePool()
    repo = Repository(pool)

    await repo.set_psn_preferences(
        sub,
        harvest_trophies=True,
        harvest_identity=True,
        harvest_presence=True,
        harvest_devices=True,
    )

    _, params = pool.connections[0].executed[0]
    assert params == (True, True, True, True, False, False, sub)


async def test_set_psn_preferences_noops_without_raising_when_unlinked():
    pool = FakePool()
    repo = Repository(pool)

    await repo.set_psn_preferences(
        new_identity_sub(),
        harvest_trophies=True,
        harvest_identity=True,
        harvest_presence=True,
        harvest_devices=True,
    )

    conn = pool.connections[0]
    assert len(conn.executed) == 1


async def test_delete_link_executes_delete():
    sub = new_identity_sub()
    pool = FakePool()
    repo = Repository(pool)

    await repo.delete_link(sub)

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "DELETE FROM psn_links WHERE identity_sub = %s" in sql
    assert params == (sub,)


async def test_each_method_call_checks_out_its_own_connection():
    sub = new_identity_sub()
    pool = FakePool()
    repo = Repository(pool)

    await repo.upsert_user(sub)
    await repo.touch_login(sub)

    assert len(pool.connections) == 2
