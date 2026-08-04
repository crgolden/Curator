"""Tests for LibraryRepository, using hand-written fake async psycopg_pool objects."""

from __future__ import annotations

from curator.library.repository import LibraryRepository


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection
        # psycopg exposes rowcount as a plain attribute; refresh_trophy_progress/clear_trophy_progress
        # read it to report how many entries they touched.
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
        title_id="t1",
    )

    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO library_entries" in sql
    assert "ON CONFLICT (identity_sub, game_id) DO UPDATE" in sql
    assert params == ("sub-1", "game-1", True, False, "God of War", "e1", "p1", "t1", True)


async def test_upsert_entry_refreshes_is_active_on_conflict():
    """A lapsed title must flip to inactive on the next build, not keep its old value.

    ``is_active`` has to be in the DO UPDATE list, not just the INSERT: the row already exists for any
    game the user has owned before, so an INSERT-only column would never change after first sight -- the
    exact staleness that left lapsed PS Plus titles stranded as owned.
    """
    pool = FakePool()
    repo = LibraryRepository(pool)

    await repo.upsert_entry(
        "sub-1",
        "game-1",
        native_ps5=True,
        ps4_eligible=False,
        owned_edition="Lapsed Title",
        winning_entitlement_id="e1",
        product_id="p1",
        title_id="t1",
        is_active=False,
    )

    sql, params = pool.connections[0].executed[0]
    assert "is_active = EXCLUDED.is_active" in sql
    assert params is not None
    assert params[8] is False


async def test_list_entries_maps_rows():
    pool = FakePool(fetchall_results=[[("sub-1", "game-1", True, False, "God of War", "e1", "p1", "t1")]])
    repo = LibraryRepository(pool)

    entries = await repo.list_entries("sub-1")

    assert len(entries) == 1
    assert entries[0].game_id == "game-1"
    assert entries[0].native_ps5 is True
    assert entries[0].ps4_eligible is False
    assert entries[0].owned_edition == "God of War"
    assert entries[0].product_id == "p1"
    assert entries[0].title_id == "t1"


async def test_list_entries_empty_when_no_rows():
    repo = LibraryRepository(FakePool(fetchall_results=[[]]))

    assert await repo.list_entries("sub-1") == []


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
                    "https://cdn.example/elden-ring.jpg",
                ),
                ("game-2", "Unmatched", None, None, None, None, None, False, False, False, None, None, None),
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
    # Read straight off the row -- no PSN call, no name matching on this path.
    assert games[0].percent_completed == 63
    assert games[0].cover_image_url == "https://cdn.example/elden-ring.jpg"
    assert games[1].percent_completed is None
    assert games[1].category is None
    assert games[1].cover_image_url is None
    # A lapsed entitlement stays listed and is flagged, rather than vanishing from the library.
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


async def test_get_games_for_continuation_maps_rows():
    pool = FakePool(fetchall_results=[[("game-1", "Elden Ring", "product-1", "title-1", True)]])
    repo = LibraryRepository(pool)

    games = await repo.get_games_for_continuation("sub-1", ["game-1"])

    assert len(games) == 1
    assert games[0].game_id == "game-1"
    assert games[0].title == "Elden Ring"
    assert games[0].product_id == "product-1"
    assert games[0].title_id == "title-1"
    assert games[0].native_ps5 is True
    sql, params = pool.connections[0].executed[0]
    assert "library_entries" in sql
    assert params == ("sub-1", ["game-1"])


async def test_get_unmatched_game_ids_returns_rows_with_no_persisted_match():
    pool = FakePool(fetchall_results=[[("game-1",), ("game-2",)]])
    repo = LibraryRepository(pool)

    result = await repo.get_unmatched_game_ids("sub-1", ["game-1", "game-2", "game-3"])

    assert result == ["game-1", "game-2"]
    sql, params = pool.connections[0].executed[0]
    assert "np_communication_id IS NULL" in sql
    assert params == ("sub-1", ["game-1", "game-2", "game-3"])


async def test_get_unmatched_game_ids_empty_input_short_circuits():
    pool = FakePool()
    repo = LibraryRepository(pool)

    assert await repo.get_unmatched_game_ids("sub-1", []) == []
    assert pool.connections == []


async def test_set_trophy_match_persists_exact_match():
    pool = FakePool()
    repo = LibraryRepository(pool)

    await repo.set_trophy_match(
        "sub-1", "game-1", np_communication_id="NPWR12345_00", method="exact", percent_completed=63
    )

    sql, params = pool.connections[0].executed[0]
    assert "UPDATE library_entries" in sql
    assert "trophy_match_attempted_at = now()" in sql
    # percent_completed appears twice: once as the value, once driving the CASE that decides whether to
    # stamp trophy_progress_fetched_at.
    assert params == ("NPWR12345_00", "exact", 63, 63, "sub-1", "game-1")


async def test_set_trophy_match_persists_no_match_found():
    # Stamping trophy_match_attempted_at even on a no-match result is what keeps a genuinely trophy-less
    # title (an app, an F2P game) from being re-attempted on every future refresh forever.
    pool = FakePool()
    repo = LibraryRepository(pool)

    await repo.set_trophy_match("sub-1", "game-1", np_communication_id=None, method=None)

    sql, params = pool.connections[0].executed[0]
    assert params == (None, None, None, None, "sub-1", "game-1")
    # A no-match attempt must not stamp a progress timestamp -- that column means "this percentage was
    # true as of then", and there is no percentage here.
    assert "ELSE now() END" in sql


async def test_refresh_trophy_progress_updates_by_np_communication_id():
    pool = FakePool()
    repo = LibraryRepository(pool)

    await repo.refresh_trophy_progress("sub-1", {"NPWR00001_00": 40, "NPWR00002_00": 90})

    executed = pool.connections[0].executed
    assert len(executed) == 2
    assert "trophy_progress_fetched_at = now()" in executed[0][0]
    assert [call[1] for call in executed] == [(40, "sub-1", "NPWR00001_00"), (90, "sub-1", "NPWR00002_00")]


async def test_refresh_trophy_progress_short_circuits_on_empty_input():
    pool = FakePool()
    repo = LibraryRepository(pool)

    assert await repo.refresh_trophy_progress("sub-1", {}) == 0
    assert pool.connections == []


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


async def test_get_games_for_continuation_empty_game_ids_short_circuits_without_a_query():
    pool = FakePool()
    repo = LibraryRepository(pool)

    games = await repo.get_games_for_continuation("sub-1", [])

    assert games == []
    assert pool.connections == []
