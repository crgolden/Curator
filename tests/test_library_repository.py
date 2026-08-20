"""Tests for LibraryRepository, using hand-written fake async psycopg_pool objects."""

from __future__ import annotations

from curator.catalog.cover_art import SQUARE_COVER_ART_SQL
from curator.library.repository import _OWNED_PLATFORMS_SQL, LibraryRepository


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection
        self.rowcount = connection.rowcount

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
    def __init__(self, fetchall_results=None, fetchone_results=None, rowcount=0):
        self.executed: list[tuple[str, tuple | None]] = []
        self.fetchall_results = list(fetchall_results or [])
        self.fetchone_results = list(fetchone_results or [])
        self.rowcount = rowcount

    def cursor(self):
        return FakeCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, fetchall_results=None, fetchone_results=None, rowcount=0):
        self._fetchall_results = fetchall_results or []
        self._fetchone_results = fetchone_results or []
        self._rowcount = rowcount
        self.connections: list[FakeConnection] = []

    def connection(self):
        conn = FakeConnection(
            fetchall_results=list(self._fetchall_results),
            fetchone_results=list(self._fetchone_results),
            rowcount=self._rowcount,
        )
        self.connections.append(conn)
        return conn


async def test_upsert_manual_entry_records_platforms_only_when_a_row_was_written():
    pool = FakePool(rowcount=0)
    repo = LibraryRepository(pool)

    await repo.upsert_manual_entry("sub-1", "game-1", native_ps5=True, ps4_eligible=False, owned_edition=None)

    executed = pool.connections[0].executed
    assert not any("library_entry_platforms" in sql for sql, _ in executed)


async def test_list_entries_with_enrichment_maps_rows_and_total():
    pool = FakePool(
        fetchone_results=[(2,)],
        fetchall_results=[
            [
                (
                    "game-1",
                    "Elden Ring",
                    "Action RPG",
                    96.0,
                    94.0,
                    4.8,
                    "product-1",
                    True,
                    True,
                    True,
                    "NPWR12345_00",
                    63,
                    "psn",
                    "https://cdn.example/elden-ring.jpg",
                    ["PS5", "PS4"],
                ),
                (
                    "game-2",
                    "Unmatched",
                    None,
                    None,
                    None,
                    None,
                    None,
                    False,
                    False,
                    False,
                    None,
                    None,
                    "manual",
                    None,
                    [],
                ),
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
    assert games[0].is_active is True
    assert games[0].np_communication_id == "NPWR12345_00"

    assert games[0].percent_completed == 63
    assert games[0].cover_image_url == "https://cdn.example/elden-ring.jpg"
    assert games[1].percent_completed is None
    assert games[1].category is None
    assert games[1].cover_image_url is None
    assert games[0].source == "psn"
    assert games[1].source == "manual", "provenance must survive the row mapping so the UI can mark it"
    assert games[0].platforms == ("PS5", "PS4")
    assert games[1].platforms == ()

    assert games[1].is_active is False
    assert games[1].np_communication_id is None


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


async def test_list_entries_with_enrichment_sorts_by_percent_completed():
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    await repo.list_entries_with_enrichment("sub-1", sort="percent_completed", sort_dir="desc")

    select_sql, _ = pool.connections[0].executed[1]
    assert "ORDER BY le.trophy_percent_completed DESC NULLS LAST, g.canonical_title ASC" in select_sql
    assert "cover_image_url" in select_sql


async def test_list_entries_with_enrichment_reads_platforms_as_a_scalar_subquery():
    """A game owned on three platforms must stay one row. Joining ``library_entry_platforms`` into the
    shared ``FROM`` clause would multiply it and inflate the ``COUNT(*)`` taken over the same clause, so
    the page would claim more games than the user owns."""
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    await repo.list_entries_with_enrichment("sub-1")

    count_sql, _ = pool.connections[0].executed[0]
    select_sql, _ = pool.connections[0].executed[1]
    assert "library_entry_platforms" not in count_sql, "the platform lookup must not reach the counted FROM clause"
    assert _OWNED_PLATFORMS_SQL in select_sql
    assert "ORDER BY p.sort_order" in select_sql


async def test_list_entries_with_enrichment_defaults_absent_platforms_to_empty():
    """``array_agg`` yields NULL over no rows; the caller must see an empty tuple, not ``None``."""
    row = ("game-1", "Solo", None, None, None, None, None, False, False, True, None, None, "manual", None, None)
    pool = FakePool(fetchone_results=[(1,)], fetchall_results=[[row]])
    repo = LibraryRepository(pool)

    games, _total = await repo.list_entries_with_enrichment("sub-1")

    assert games[0].platforms == ()


async def test_list_entries_with_enrichment_uses_the_shared_cover_art_expression():
    """The library table shows the same games as the catalog, so it must resolve artwork identically --
    ``psn_catalog_cache``'s 16:9 key art would render this table at a different shape from every other
    surface."""
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    await repo.list_entries_with_enrichment("sub-1")

    select_sql, _ = pool.connections[0].executed[1]
    assert SQUARE_COVER_ART_SQL in select_sql
    assert "psn_catalog_cache" not in select_sql


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


async def test_clear_trophy_progress_erases_percentages_but_keeps_the_match():
    """Opting out of trophy harvesting must remove what was already collected, not just stop collecting.

    np_communication_id deliberately survives: it is an id-to-id mapping, not PSN activity data, and
    keeping it means re-enabling the preference doesn't have to re-run the whole matching pass.
    """
    pool = FakePool()
    repo = LibraryRepository(pool)

    await repo.clear_trophy_progress("sub-1")

    sql, params = pool.connections[0].executed[0]
    assert "trophy_percent_completed = NULL" in sql
    assert "trophy_progress_fetched_at = NULL" in sql
    assert "np_communication_id" not in sql
    assert params == ("sub-1",)
