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

    await repository.upsert_manual_entry(
        "sub-a", "game-1", native_ps5=True, ps4_eligible=False, owned_edition="Standard"
    )

    sql, params = pool.connections[0].executed[0]
    assert "'manual'" in sql
    assert "winning_entitlement_id" not in sql, "a manual row has no entitlement to record"
    assert params == ("sub-a", "game-1", True, False, "Standard")


async def test_manual_entry_cannot_overwrite_a_psn_sourced_row():
    pool = FakePool()
    repository = LibraryRepository(pool)

    await repository.upsert_manual_entry("sub-a", "game-1", native_ps5=False, ps4_eligible=True, owned_edition=None)

    sql, _ = pool.connections[0].executed[0]
    assert "WHERE library_entries.source = 'manual'" in sql, (
        "without this guard, adding a game by hand would downgrade a real entitlement-backed row"
    )


async def test_psn_upsert_claims_a_row_that_had_been_added_manually():
    pool = FakePool()
    repository = LibraryRepository(pool)

    await repository.upsert_entry(
        "sub-a",
        "game-1",
        native_ps5=True,
        ps4_eligible=False,
        owned_edition="Deluxe",
        winning_entitlement_id="ent-1",
        product_id="prod-1",
        title_id="CUSA1",
    )

    sql, _ = pool.connections[0].executed[0]
    assert "source = 'psn'" in sql, "buying a manually-added game digitally makes it a PSN row"


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


async def test_a_psn_refresh_never_deactivates_rows_it_did_not_mention():
    pool = FakePool()
    repository = LibraryRepository(pool)

    await repository.upsert_entry(
        "sub-a",
        "game-1",
        native_ps5=False,
        ps4_eligible=True,
        owned_edition=None,
        winning_entitlement_id="ent-1",
        product_id=None,
        title_id=None,
        is_active=True,
    )

    for sql, params in pool.connections[0].executed:
        assert "is_active = false" not in sql.lower()
        assert params is not None
        assert "game-1" in params, "every statement must be scoped to the pulled game"
