"""Tests for manually-tracked (non-entitlement) library entries, using hand-written fake psycopg objects."""

from __future__ import annotations

from curator.library.repository import LibraryRepository
from curator.psn.title_platform import PS3, PS4, PS5
from test_values import lowercase_token, new_game_id, new_identity_sub


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
    sub = new_identity_sub()
    game_id = new_game_id()
    owned_edition = lowercase_token()
    pool = FakePool()
    repository = LibraryRepository(pool)

    await repository.upsert_manual_entry(sub, game_id, platforms=(PS5,), owned_edition=owned_edition)

    sql, params = pool.connections[0].executed[0]
    assert "'manual'" in sql
    assert "winning_entitlement_id" not in sql, "a manual row has no entitlement to record"
    assert params == (sub, game_id, owned_edition)


async def test_manual_entry_cannot_overwrite_a_psn_sourced_row():
    pool = FakePool()
    repository = LibraryRepository(pool)

    await repository.upsert_manual_entry(new_identity_sub(), new_game_id(), platforms=(PS4,), owned_edition=None)

    sql, _ = pool.connections[0].executed[0]
    assert "WHERE library_entries.source = 'manual'" in sql, (
        "without this guard, adding a game by hand would downgrade a real entitlement-backed row"
    )


async def test_a_declined_manual_upsert_reports_that_it_wrote_nothing():
    """The guard above has always declined silently. A caller that cannot tell 'added' from 'already
    yours' reports success for a write that never happened -- which is what a lapsed PSN entitlement
    looks like, since its row is inactive and easy to miss in the table."""
    pool = FakePool(rowcount=0)
    repository = LibraryRepository(pool)

    written = await repository.upsert_manual_entry(
        new_identity_sub(), new_game_id(), platforms=(PS4,), owned_edition=None
    )

    assert written is False
    assert len(pool.connections[0].executed) == 1, "a declined upsert must not go on to rewrite platforms"


async def test_a_written_manual_upsert_reports_success_and_reconciles_platforms():
    pool = FakePool(rowcount=1)
    repository = LibraryRepository(pool)

    written = await repository.upsert_manual_entry(
        new_identity_sub(), new_game_id(), platforms=(PS4,), owned_edition=None
    )

    assert written is True
    assert len(pool.connections[0].executed) > 1, "a written upsert reconciles library_entry_platforms"


async def test_a_platform_is_recorded_only_in_the_platforms_table():
    sub = new_identity_sub()
    game_id = new_game_id()
    pool = FakePool(rowcount=1)
    repository = LibraryRepository(pool)

    await repository.upsert_manual_entry(sub, game_id, platforms=(PS3,), owned_edition=None)

    upsert_sql, upsert_params = pool.connections[0].executed[0]
    assert "native_ps5" not in upsert_sql
    assert "ps4_eligible" not in upsert_sql
    assert upsert_params == (sub, game_id, None)
    delete_sql, delete_params = pool.connections[0].executed[1]
    assert "DELETE FROM library_entry_platforms" in delete_sql
    assert delete_params == (sub, game_id, [PS3])
    insert_sql, insert_params = pool.connections[0].executed[2]
    assert "INSERT INTO library_entry_platforms" in insert_sql
    assert insert_params == (sub, game_id, [PS3]), (
        "library_entry_platforms is the platform of record; the boolean pair cannot carry this row"
    )


async def test_platform_rows_are_replaced_not_unioned():
    sub = new_identity_sub()
    game_id = new_game_id()
    pool = FakePool(rowcount=1)
    repository = LibraryRepository(pool)

    await repository.upsert_manual_entry(sub, game_id, platforms=(PS5, PS4, PS5), owned_edition=None)

    delete_sql, delete_params = pool.connections[0].executed[1]
    assert "NOT (platform = ANY(%s::text[]))" in delete_sql, (
        "a platform the caller dropped must be deleted, or an edit could only ever add"
    )
    assert delete_params == (sub, game_id, [PS5, PS4]), "duplicates collapse"


async def test_platform_rows_are_left_alone_when_the_upsert_matched_no_row():
    pool = FakePool(rowcount=0)
    repository = LibraryRepository(pool)

    await repository.upsert_manual_entry(new_identity_sub(), new_game_id(), platforms=(PS5,), owned_edition=None)

    assert len(pool.connections[0].executed) == 1, (
        "the upsert is guarded on source = 'manual', so a PSN-sourced row must not have its platforms rewritten"
    )


async def test_deleting_a_manual_entry_cannot_touch_a_psn_row():
    sub = new_identity_sub()
    game_id = new_game_id()
    pool = FakePool(rowcount=1)
    repository = LibraryRepository(pool)

    removed = await repository.delete_manual_entry(sub, game_id)

    assert removed is True
    sql, params = pool.connections[0].executed[0]
    assert sql.strip().startswith("DELETE FROM library_entries")
    assert "source = 'manual'" in sql
    assert params == (sub, game_id)


async def test_deleting_a_game_with_no_manual_entry_reports_false():
    pool = FakePool(rowcount=0)
    repository = LibraryRepository(pool)

    assert await repository.delete_manual_entry(new_identity_sub(), new_game_id()) is False
