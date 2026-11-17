"""Tests for EnrichmentKeysRepository, using the same hand-written fake pool/connection/cursor pattern as
test_repository.py (no real database, no unittest.mock)."""

from __future__ import annotations

from curator.persistence.enrichment_keys_repository import EnrichmentKeysRepository
from test_values import new_identity_sub, new_opaque_token, new_utc_instant


class FakeCursor:
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
    def __init__(self, fetchone_result=None) -> None:
        self._fetchone_result = fetchone_result
        self.connections: list[FakeConnection] = []

    def connection(self) -> FakeConnection:
        conn = FakeConnection(fetchone_result=self._fetchone_result)
        self.connections.append(conn)
        return conn


def _encrypted_key() -> bytes:
    return new_opaque_token().encode()


async def test_get_status_returns_both_false_when_no_row():
    pool = FakePool(fetchone_result=None)
    repo = EnrichmentKeysRepository(pool)

    status = await repo.get_status(new_identity_sub())

    assert status.rawg_configured is False
    assert status.opencritic_configured is False
    assert status.rawg_added_at is None
    assert status.opencritic_added_at is None
    assert status.rawg_key_rejected_at is None
    assert status.opencritic_key_rejected_at is None


async def test_get_status_maps_row():
    rawg_added = new_utc_instant()
    oc_added = new_utc_instant()
    row = (_encrypted_key(), _encrypted_key(), rawg_added, oc_added, None, None)
    pool = FakePool(fetchone_result=row)
    repo = EnrichmentKeysRepository(pool)

    status = await repo.get_status(new_identity_sub())

    assert status.rawg_configured is True
    assert status.opencritic_configured is True
    assert status.rawg_added_at == rawg_added
    assert status.opencritic_added_at == oc_added
    assert status.rawg_key_rejected_at is None
    assert status.opencritic_key_rejected_at is None


async def test_get_status_treats_null_column_as_not_configured():
    row = (None, _encrypted_key(), None, new_utc_instant(), None, None)
    pool = FakePool(fetchone_result=row)
    repo = EnrichmentKeysRepository(pool)

    status = await repo.get_status(new_identity_sub())

    assert status.rawg_configured is False
    assert status.opencritic_configured is True


async def test_get_status_maps_rejected_at_columns():
    rejected = new_utc_instant()
    row = (_encrypted_key(), None, new_utc_instant(), None, rejected, None)
    pool = FakePool(fetchone_result=row)
    repo = EnrichmentKeysRepository(pool)

    status = await repo.get_status(new_identity_sub())

    assert status.rawg_configured is True
    assert status.rawg_key_rejected_at == rejected
    assert status.opencritic_key_rejected_at is None


async def test_get_decrypted_key_material_returns_none_none_when_no_row():
    pool = FakePool(fetchone_result=None)
    repo = EnrichmentKeysRepository(pool)

    assert await repo.get_decrypted_key_material(new_identity_sub()) == (None, None)


async def test_get_decrypted_key_material_returns_raw_bytes():
    rawg_key = _encrypted_key()
    pool = FakePool(fetchone_result=(rawg_key, None))
    repo = EnrichmentKeysRepository(pool)

    assert await repo.get_decrypted_key_material(new_identity_sub()) == (rawg_key, None)


async def test_upsert_rawg_key_executes_upsert():
    sub = new_identity_sub()
    rawg_key = _encrypted_key()
    pool = FakePool()
    repo = EnrichmentKeysRepository(pool)

    await repo.upsert_rawg_key(sub, rawg_key)

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "INSERT INTO user_enrichment_keys" in sql
    assert "ON CONFLICT (identity_sub) DO UPDATE" in sql
    assert "rawg_api_key_enc" in sql
    assert "rawg_key_rejected_at = NULL" in sql
    assert params == (sub, rawg_key)


async def test_upsert_opencritic_key_executes_upsert():
    sub = new_identity_sub()
    opencritic_key = _encrypted_key()
    pool = FakePool()
    repo = EnrichmentKeysRepository(pool)

    await repo.upsert_opencritic_key(sub, opencritic_key)

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "opencritic_api_key_enc" in sql
    assert "opencritic_key_rejected_at = NULL" in sql
    assert params == (sub, opencritic_key)


async def test_mark_rawg_key_rejected_updates_only_rawg_rejected_at():
    sub = new_identity_sub()
    pool = FakePool()
    repo = EnrichmentKeysRepository(pool)

    await repo.mark_rawg_key_rejected(sub)

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "rawg_key_rejected_at = now()" in sql
    assert "opencritic_key_rejected_at" not in sql
    assert params == (sub,)


async def test_mark_opencritic_key_rejected_updates_only_opencritic_rejected_at():
    sub = new_identity_sub()
    pool = FakePool()
    repo = EnrichmentKeysRepository(pool)

    await repo.mark_opencritic_key_rejected(sub)

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "opencritic_key_rejected_at = now()" in sql
    assert "rawg_key_rejected_at" not in sql
    assert params == (sub,)


async def test_delete_rawg_key_clears_only_rawg_columns():
    sub = new_identity_sub()
    pool = FakePool()
    repo = EnrichmentKeysRepository(pool)

    await repo.delete_rawg_key(sub)

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "rawg_api_key_enc = NULL" in sql
    assert "rawg_added_at = NULL" in sql
    assert "opencritic_api_key_enc" not in sql
    assert params == (sub,)


async def test_delete_opencritic_key_clears_only_opencritic_columns():
    sub = new_identity_sub()
    pool = FakePool()
    repo = EnrichmentKeysRepository(pool)

    await repo.delete_opencritic_key(sub)

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "opencritic_api_key_enc = NULL" in sql
    assert "opencritic_added_at = NULL" in sql
    assert "rawg_api_key_enc" not in sql
    assert params == (sub,)
