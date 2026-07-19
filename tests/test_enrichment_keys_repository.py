"""Tests for EnrichmentKeysRepository, using the same hand-written fake pool/connection/cursor pattern as
test_repository.py (no real database, no unittest.mock)."""

from __future__ import annotations

from datetime import datetime, timezone

from curator.persistence.enrichment_keys_repository import EnrichmentKeysRepository


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


async def test_get_status_returns_both_false_when_no_row():
    pool = FakePool(fetchone_result=None)
    repo = EnrichmentKeysRepository(pool)

    status = await repo.get_status("sub-1")

    assert status.rawg_configured is False
    assert status.opencritic_configured is False
    assert status.rawg_added_at is None
    assert status.opencritic_added_at is None


async def test_get_status_maps_row():
    rawg_added = datetime(2026, 1, 1, tzinfo=timezone.utc)
    oc_added = datetime(2026, 2, 1, tzinfo=timezone.utc)
    row = (b"rawg-enc", b"oc-enc", rawg_added, oc_added)
    pool = FakePool(fetchone_result=row)
    repo = EnrichmentKeysRepository(pool)

    status = await repo.get_status("sub-1")

    assert status.rawg_configured is True
    assert status.opencritic_configured is True
    assert status.rawg_added_at == rawg_added
    assert status.opencritic_added_at == oc_added


async def test_get_status_treats_null_column_as_not_configured():
    row = (None, b"oc-enc", None, datetime(2026, 2, 1, tzinfo=timezone.utc))
    pool = FakePool(fetchone_result=row)
    repo = EnrichmentKeysRepository(pool)

    status = await repo.get_status("sub-1")

    assert status.rawg_configured is False
    assert status.opencritic_configured is True


async def test_get_decrypted_key_material_returns_none_none_when_no_row():
    pool = FakePool(fetchone_result=None)
    repo = EnrichmentKeysRepository(pool)

    assert await repo.get_decrypted_key_material("sub-1") == (None, None)


async def test_get_decrypted_key_material_returns_raw_bytes():
    pool = FakePool(fetchone_result=(b"rawg-enc", None))
    repo = EnrichmentKeysRepository(pool)

    assert await repo.get_decrypted_key_material("sub-1") == (b"rawg-enc", None)


async def test_upsert_rawg_key_executes_upsert():
    pool = FakePool()
    repo = EnrichmentKeysRepository(pool)

    await repo.upsert_rawg_key("sub-1", b"rawg-enc")

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "INSERT INTO user_enrichment_keys" in sql
    assert "ON CONFLICT (identity_sub) DO UPDATE" in sql
    assert "rawg_api_key_enc" in sql
    assert params == ("sub-1", b"rawg-enc")


async def test_upsert_opencritic_key_executes_upsert():
    pool = FakePool()
    repo = EnrichmentKeysRepository(pool)

    await repo.upsert_opencritic_key("sub-1", b"oc-enc")

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "opencritic_api_key_enc" in sql
    assert params == ("sub-1", b"oc-enc")


async def test_delete_rawg_key_clears_only_rawg_columns():
    pool = FakePool()
    repo = EnrichmentKeysRepository(pool)

    await repo.delete_rawg_key("sub-1")

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "rawg_api_key_enc = NULL" in sql
    assert "rawg_added_at = NULL" in sql
    assert "opencritic_api_key_enc" not in sql
    assert params == ("sub-1",)


async def test_delete_opencritic_key_clears_only_opencritic_columns():
    pool = FakePool()
    repo = EnrichmentKeysRepository(pool)

    await repo.delete_opencritic_key("sub-1")

    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "opencritic_api_key_enc = NULL" in sql
    assert "opencritic_added_at = NULL" in sql
    assert "rawg_api_key_enc" not in sql
    assert params == ("sub-1",)
