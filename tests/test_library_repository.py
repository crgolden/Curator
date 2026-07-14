"""Tests for LibraryRepository, using hand-written fake async psycopg_pool objects."""

from __future__ import annotations

from curator.library.repository import LibraryRepository


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection

    async def execute(self, sql, params=None):
        self._connection.executed.append((sql, params))

    async def fetchall(self):
        if self._connection.fetchall_results:
            return self._connection.fetchall_results.pop(0)
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, fetchall_results=None):
        self.executed: list[tuple[str, tuple | None]] = []
        self.fetchall_results = list(fetchall_results or [])

    def cursor(self):
        return FakeCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, fetchall_results=None):
        self._fetchall_results = fetchall_results or []
        self.connections: list[FakeConnection] = []

    def connection(self):
        conn = FakeConnection(fetchall_results=list(self._fetchall_results))
        self.connections.append(conn)
        return conn


async def test_upsert_entry_executes_upsert():
    pool = FakePool()
    repo = LibraryRepository(pool)

    await repo.upsert_entry(
        "sub-1",
        "game-1",
        native_ps5=True,
        ps4_eligible=False,
        owned_edition="God of War",
        winning_entitlement_id="e1",
        product_id="p1",
    )

    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO library_entries" in sql
    assert "ON CONFLICT (identity_sub, game_id) DO UPDATE" in sql
    assert params == ("sub-1", "game-1", True, False, "God of War", "e1", "p1")


async def test_list_entries_maps_rows():
    pool = FakePool(fetchall_results=[[("sub-1", "game-1", True, False, "God of War", "e1", "p1")]])
    repo = LibraryRepository(pool)

    entries = await repo.list_entries("sub-1")

    assert len(entries) == 1
    assert entries[0].game_id == "game-1"
    assert entries[0].native_ps5 is True
    assert entries[0].ps4_eligible is False
    assert entries[0].owned_edition == "God of War"


async def test_list_entries_empty_when_no_rows():
    repo = LibraryRepository(FakePool(fetchall_results=[[]]))

    assert await repo.list_entries("sub-1") == []
