"""Tests for manually-tracked (non-entitlement) library entries, using hand-written fake psycopg objects."""

from __future__ import annotations

from curator.library.repository import LibraryRepository


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection
        self.rowcount = connection.rowcount

    async def execute(self, sql, params=None):
        self._connection.executed.append((sql, params))

    async def fetchone(self):
        return None

    async def fetchall(self):
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, rowcount=0):
        self.executed: list[tuple[str, tuple | None]] = []
        self.rowcount = rowcount

    def cursor(self):
        return FakeCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, rowcount=0):
        self._rowcount = rowcount
        self.connections: list[FakeConnection] = []

    def connection(self):
        conn = FakeConnection(rowcount=self._rowcount)
        self.connections.append(conn)
        return conn


async def test_manual_entry_is_written_with_manual_provenance_and_no_entitlement():
    pool = FakePool()
    repository = LibraryRepository(pool)

    await repository.upsert_manual_entry("sub-a", "game-1", platforms=("PS5",), owned_edition="Standard")

    sql, params = pool.connections[0].executed[0]
    assert "'manual'" in sql
    assert "winning_entitlement_id" not in sql, "a manual row has no entitlement to record"
    assert params == ("sub-a", "game-1", True, False, "Standard")


async def test_manual_entry_cannot_overwrite_a_psn_sourced_row():
    pool = FakePool()
    repository = LibraryRepository(pool)

    await repository.upsert_manual_entry("sub-a", "game-1", platforms=("PS4",), owned_edition=None)

    sql, _ = pool.connections[0].executed[0]
    assert "WHERE library_entries.source = 'manual'" in sql, (
        "without this guard, adding a game by hand would downgrade a real entitlement-backed row"
    )


async def test_a_legacy_platform_leaves_both_booleans_false_and_still_records_the_platform():
    pool = FakePool(rowcount=1)
    repository = LibraryRepository(pool)

    await repository.upsert_manual_entry("sub-a", "game-1", platforms=("PS3",), owned_edition=None)

    _, upsert_params = pool.connections[0].executed[0]
    assert upsert_params == ("sub-a", "game-1", False, False, None), (
        "native_ps5/ps4_eligible have no spelling for PS3, so leaving both false is correct rather than lossy"
    )
    delete_sql, delete_params = pool.connections[0].executed[1]
    assert "DELETE FROM library_entry_platforms" in delete_sql
    assert delete_params == ("sub-a", "game-1", ["PS3"])
    insert_sql, insert_params = pool.connections[0].executed[2]
    assert "INSERT INTO library_entry_platforms" in insert_sql
    assert insert_params == ("sub-a", "game-1", ["PS3"]), (
        "library_entry_platforms is the platform of record; the boolean pair cannot carry this row"
    )


async def test_platform_rows_are_replaced_not_unioned():
    pool = FakePool(rowcount=1)
    repository = LibraryRepository(pool)

    await repository.upsert_manual_entry("sub-a", "game-1", platforms=("PS5", "PS4", "PS5"), owned_edition=None)

    delete_sql, delete_params = pool.connections[0].executed[1]
    assert "NOT (platform = ANY(%s::text[]))" in delete_sql, (
        "a platform the caller dropped must be deleted, or an edit could only ever add"
    )
    assert delete_params == ("sub-a", "game-1", ["PS5", "PS4"]), "duplicates collapse"


async def test_platform_rows_are_left_alone_when_the_upsert_matched_no_row():
    pool = FakePool(rowcount=0)
    repository = LibraryRepository(pool)

    await repository.upsert_manual_entry("sub-a", "game-1", platforms=("PS5",), owned_edition=None)

    assert len(pool.connections[0].executed) == 1, (
        "the upsert is guarded on source = 'manual', so a PSN-sourced row must not have its platforms rewritten"
    )


async def test_deleting_a_manual_entry_cannot_touch_a_psn_row():
    pool = FakePool(rowcount=1)
    repository = LibraryRepository(pool)

    removed = await repository.delete_manual_entry("sub-a", "game-1")

    assert removed is True
    sql, params = pool.connections[0].executed[0]
    assert sql.strip().startswith("DELETE FROM library_entries")
    assert "source = 'manual'" in sql
    assert params == ("sub-a", "game-1")


async def test_deleting_a_game_with_no_manual_entry_reports_false():
    pool = FakePool(rowcount=0)
    repository = LibraryRepository(pool)

    assert await repository.delete_manual_entry("sub-a", "game-1") is False
