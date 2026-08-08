"""Tests for CollectionsRepository.list_definition_items_page, using hand-written fake psycopg_pool objects."""

from __future__ import annotations

import pytest

from curator.collections.repository import CollectionsRepository


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection
        self.rowcount = connection.rowcount

    async def execute(self, sql, params=None):
        self._connection.executed.append((sql, params))

    async def fetchone(self):
        if self._connection.fetchone_results:
            return self._connection.fetchone_results.pop(0)
        return None

    async def fetchall(self):
        if self._connection.fetchall_results:
            return self._connection.fetchall_results.pop(0)
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, fetchone_results=None, fetchall_results=None, rowcount=0):
        self.executed: list[tuple[str, tuple | None]] = []
        self.fetchone_results = list(fetchone_results or [])
        self.fetchall_results = list(fetchall_results or [])
        self.rowcount = rowcount

    def cursor(self):
        return FakeCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, fetchone_results=None, fetchall_results=None, rowcount=0):
        self._fetchone_results = fetchone_results or []
        self._fetchall_results = fetchall_results or []
        self._rowcount = rowcount
        self.connections: list[FakeConnection] = []

    def connection(self):
        conn = FakeConnection(
            fetchone_results=list(self._fetchone_results),
            fetchall_results=list(self._fetchall_results),
            rowcount=self._rowcount,
        )
        self.connections.append(conn)
        return conn


def _item_row(game_id="11111111-1111-1111-1111-111111111111", rank=1, title="Bloodborne"):
    return (game_id, rank, title, "FromSoftware", "Action", "AAA", 92.0, 91.0, 4.5, "https://img/1.png", True)


def _pool_returning(rows, total):
    return FakePool(fetchone_results=[(total,)], fetchall_results=[rows])


async def test_returns_page_and_total_separately():
    pool = _pool_returning([_item_row()], total=137)
    repository = CollectionsRepository(pool)

    items, total = await repository.list_definition_items_page("def-1")

    assert total == 137, "total must come from COUNT(*), not the length of the page"
    assert len(items) == 1
    assert items[0].title == "Bloodborne"
    assert items[0].owner_has_access is True


async def test_counts_the_filtered_set_not_the_whole_collection():
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page("def-1", search="souls", genre="RPG")

    count_sql, count_params = pool.connections[0].executed[0]
    assert "COUNT(*)" in count_sql
    assert "ILIKE" in count_sql, "the title filter must apply to the count as well as the page"
    assert "gen.name = %s" in count_sql
    assert count_params == ("def-1", "%souls%", "RPG")


async def test_defaults_to_rank_order_so_the_unpaged_route_is_unchanged():
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page("def-1")

    page_sql, _ = pool.connections[0].executed[1]
    assert "ORDER BY cdi.rank ASC" in page_sql


@pytest.mark.parametrize(
    ("sort", "expected_column"),
    [
        ("rank", "cdi.rank"),
        ("title", "g.canonical_title"),
        ("critical_score", "ge.critical_score"),
        ("oc_score", "ge.oc_score"),
        ("psn_rating", "ge.psn_rating"),
    ],
)
async def test_every_sort_field_resolves_to_its_allow_listed_column(sort, expected_column):
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page("def-1", sort=sort)

    page_sql, _ = pool.connections[0].executed[1]
    assert f"ORDER BY {expected_column} ASC" in page_sql


async def test_unknown_sort_field_raises_rather_than_reaching_the_sql():
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    with pytest.raises(KeyError):
        await repository.list_definition_items_page("def-1", sort="; DROP TABLE games--")

    assert pool.connections == [], "the allow-list must reject before a connection is taken"


async def test_unresolved_scores_sort_last_in_both_directions():
    for direction in ("asc", "desc"):
        pool = _pool_returning([], total=0)
        repository = CollectionsRepository(pool)

        await repository.list_definition_items_page("def-1", sort="oc_score", sort_dir=direction)

        page_sql, _ = pool.connections[0].executed[1]
        assert "NULLS LAST" in page_sql


async def test_every_sort_carries_a_deterministic_tiebreak():
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page("def-1", sort="title", sort_dir="desc")

    page_sql, _ = pool.connections[0].executed[1]
    assert page_sql.rstrip().endswith("LIMIT %s OFFSET %s")
    assert ", cdi.rank ASC" in page_sql, "without a tiebreak, OFFSET paging can repeat or skip tied rows"


async def test_unrecognised_sort_direction_falls_back_to_ascending():
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page("def-1", sort="title", sort_dir="sideways")

    page_sql, _ = pool.connections[0].executed[1]
    assert "ORDER BY g.canonical_title ASC" in page_sql


async def test_limit_and_offset_are_the_last_two_parameters():
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page("def-1", search="halo", limit=25, offset=50)

    _, page_params = pool.connections[0].executed[1]
    assert page_params == ("def-1", "%halo%", 25, 50)


async def test_removal_targets_one_row_and_never_rewrites_the_membership():
    pool = FakePool(rowcount=1)
    repository = CollectionsRepository(pool)

    removed = await repository.remove_definition_item("def-1", "game-9")

    assert removed is True
    sql, params = pool.connections[0].executed[0]
    assert sql.strip().startswith("DELETE FROM collection_definition_items")
    assert params == ("def-1", "game-9")
    assert "INSERT" not in sql, "removal must not replace the membership -- that is what paging makes unsafe"


async def test_removal_reports_false_when_the_game_was_not_a_member():
    pool = FakePool(rowcount=0)
    repository = CollectionsRepository(pool)

    assert await repository.remove_definition_item("def-1", "not-a-member") is False


async def test_page_query_is_not_scoped_to_a_viewer():
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page("def-1")

    for sql, _ in pool.connections[0].executed:
        assert "le.identity_sub = cd.identity_sub" in sql, "access resolves from the owner's sub, never a viewer's"
