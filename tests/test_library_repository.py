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

    async def fetchone(self):
        if self._connection.fetchone_results:
            return self._connection.fetchone_results.pop(0)
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, fetchall_results=None, fetchone_results=None):
        self.executed: list[tuple[str, tuple | None]] = []
        self.fetchall_results = list(fetchall_results or [])
        self.fetchone_results = list(fetchone_results or [])

    def cursor(self):
        return FakeCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, fetchall_results=None, fetchone_results=None):
        self._fetchall_results = fetchall_results or []
        self._fetchone_results = fetchone_results or []
        self.connections: list[FakeConnection] = []

    def connection(self):
        conn = FakeConnection(
            fetchall_results=list(self._fetchall_results), fetchone_results=list(self._fetchone_results)
        )
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


async def test_list_entries_with_enrichment_maps_rows_and_total():
    pool = FakePool(
        fetchone_results=[(2,)],
        fetchall_results=[
            [
                ("game-1", "Elden Ring", "Action RPG", 96.0, 94.0, 4.8, "product-1", True, True),
                ("game-2", "Unmatched", None, None, None, None, None, False, False),
            ]
        ],
    )
    repo = LibraryRepository(pool)

    games, total = await repo.list_entries_with_enrichment("sub-1")

    assert total == 2
    assert len(games) == 2
    assert games[0].game_id == "game-1"
    assert games[0].category == "Action RPG"
    assert games[0].rawg_rating == 96.0
    assert games[0].opencritic_rating == 94.0
    assert games[0].psn_rating == 4.8
    assert games[0].psn_product_id == "product-1"
    assert games[0].rawg_enriched is True
    assert games[1].category is None


async def test_list_entries_with_enrichment_builds_search_and_category_conditions():
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    await repo.list_entries_with_enrichment("sub-1", search="ring", category="Action RPG")

    count_sql, count_params = pool.connections[0].executed[0]
    assert "ILIKE" in count_sql
    assert "gen.name = %s" in count_sql
    assert count_params == ("sub-1", "%ring%", "Action RPG")

    select_sql, select_params = pool.connections[0].executed[1]
    assert "ILIKE" in select_sql
    assert select_params == ("sub-1", "%ring%", "Action RPG", 20, 0)


async def test_list_entries_with_enrichment_rejects_unknown_sort_field():
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    try:
        await repo.list_entries_with_enrichment("sub-1", sort="not_a_real_field")
    except KeyError:
        return
    raise AssertionError("expected a KeyError for an unknown sort field")


async def test_list_entries_with_enrichment_orders_by_sort_column_nulls_last():
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    await repo.list_entries_with_enrichment("sub-1", sort="psn_rating", sort_dir="desc")

    select_sql, _ = pool.connections[0].executed[1]
    assert "ORDER BY ge.psn_rating DESC NULLS LAST, g.canonical_title ASC" in select_sql


async def test_list_entries_with_enrichment_applies_limit_and_offset():
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    await repo.list_entries_with_enrichment("sub-1", limit=5, offset=10)

    _, select_params = pool.connections[0].executed[1]
    assert select_params is not None
    assert select_params[-2:] == (5, 10)


async def test_list_categories_returns_distinct_sorted_names():
    pool = FakePool(fetchall_results=[[("Puzzle",), ("RPG",)]])
    repo = LibraryRepository(pool)

    categories = await repo.list_categories("sub-1")

    assert categories == ["Puzzle", "RPG"]
    sql, params = pool.connections[0].executed[0]
    assert "SELECT DISTINCT gen.name" in sql
    assert params == ("sub-1",)


async def test_list_categories_empty_when_no_rows():
    repo = LibraryRepository(FakePool(fetchall_results=[[]]))

    assert await repo.list_categories("sub-1") == []
