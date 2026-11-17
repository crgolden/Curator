"""Tests for CollectionsRepository.list_definition_items_page, using hand-written fake psycopg_pool objects."""

from __future__ import annotations

import random
from typing import get_args

import pytest

from curator.collections.repository import _ITEM_SORT_COLUMNS, CollectionItemSortField, CollectionsRepository
from test_values import (
    lowercase_token,
    new_cover_image_url,
    new_definition_id,
    new_game_id,
    new_game_title,
    new_genre_name,
    new_positive_count,
    new_psn_rating,
    new_review_score,
)


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


def _item_row(title, rank=1, installed_on_target=None):
    return (
        new_game_id(),
        rank,
        title,
        lowercase_token(),
        new_genre_name(),
        lowercase_token(),
        new_review_score(),
        new_review_score(),
        new_psn_rating(),
        new_cover_image_url(),
        True,
        installed_on_target,
    )


def _pool_returning(rows, total):
    return FakePool(fetchone_results=[(total,)], fetchall_results=[rows])


async def test_returns_page_and_total_separately():
    title = new_game_title()
    rows = [_item_row(title)]
    expected_total = len(rows) + new_positive_count()
    pool = _pool_returning(rows, total=expected_total)
    repository = CollectionsRepository(pool)

    items, total = await repository.list_definition_items_page(new_definition_id())

    assert total == expected_total, "total must come from COUNT(*), not the length of the page"
    assert len(items) == 1
    assert items[0].title == title
    assert items[0].owner_has_access is True


async def test_counts_the_filtered_set_not_the_whole_collection():
    definition_id = new_definition_id()
    search = lowercase_token()
    genre = new_genre_name()
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page(definition_id, search=search, genre=genre)

    count_sql, count_params = pool.connections[0].executed[0]
    assert "COUNT(*)" in count_sql
    assert "ILIKE" in count_sql, "the title filter must apply to the count as well as the page"
    assert "gen.name = %s" in count_sql
    assert count_params == (definition_id, f"%{search}%", genre)


async def test_defaults_to_rank_order_so_the_unpaged_route_is_unchanged():
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page(new_definition_id())

    page_sql, _ = pool.connections[0].executed[1]
    assert "ORDER BY cdi.rank ASC" in page_sql


def test_the_sort_allow_list_covers_exactly_the_declared_sort_fields():
    assert set(_ITEM_SORT_COLUMNS) == set(get_args(CollectionItemSortField))


@pytest.mark.parametrize("sort", get_args(CollectionItemSortField))
async def test_every_sort_field_resolves_to_its_allow_listed_column(sort):
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page(new_definition_id(), sort=sort)

    page_sql, _ = pool.connections[0].executed[1]
    assert f"ORDER BY {_ITEM_SORT_COLUMNS[sort]} ASC" in page_sql


async def test_unknown_sort_field_raises_rather_than_reaching_the_sql():
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    with pytest.raises(KeyError):
        await repository.list_definition_items_page(new_definition_id(), sort=lowercase_token())

    assert pool.connections == [], "the allow-list must reject before a connection is taken"


@pytest.mark.parametrize("direction", ["asc", "desc"])
async def test_unresolved_scores_sort_last_in_both_directions(direction: str):
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page(
        new_definition_id(), sort=random.choice(get_args(CollectionItemSortField)), sort_dir=direction
    )

    page_sql, _ = pool.connections[0].executed[1]
    assert "NULLS LAST" in page_sql


async def test_every_sort_carries_a_deterministic_tiebreak():
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page(
        new_definition_id(), sort=random.choice(get_args(CollectionItemSortField)), sort_dir="desc"
    )

    page_sql, _ = pool.connections[0].executed[1]
    assert page_sql.rstrip().endswith("LIMIT %s OFFSET %s")
    assert ", cdi.rank ASC" in page_sql, "without a tiebreak, OFFSET paging can repeat or skip tied rows"


async def test_unrecognised_sort_direction_falls_back_to_ascending():
    sort = random.choice(get_args(CollectionItemSortField))
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page(new_definition_id(), sort=sort, sort_dir=lowercase_token())

    page_sql, _ = pool.connections[0].executed[1]
    assert f"ORDER BY {_ITEM_SORT_COLUMNS[sort]} ASC" in page_sql


async def test_limit_and_offset_are_the_last_two_parameters():
    definition_id = new_definition_id()
    search = lowercase_token()
    limit = new_positive_count()
    offset = limit + new_positive_count()
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page(definition_id, search=search, limit=limit, offset=offset)

    _, page_params = pool.connections[0].executed[1]
    assert page_params == (definition_id, f"%{search}%", limit, offset)


async def test_removal_targets_one_row_and_never_rewrites_the_membership():
    definition_id = new_definition_id()
    game_id = new_game_id()
    pool = FakePool(rowcount=1)
    repository = CollectionsRepository(pool)

    removed = await repository.remove_definition_item(definition_id, game_id)

    assert removed is True
    sql, params = pool.connections[0].executed[0]
    assert sql.strip().startswith("DELETE FROM collection_definition_items")
    assert params == (definition_id, game_id)
    assert "INSERT" not in sql, "removal must not replace the membership -- that is what paging makes unsafe"


async def test_removal_reports_false_when_the_game_was_not_a_member():
    pool = FakePool(rowcount=0)
    repository = CollectionsRepository(pool)

    assert await repository.remove_definition_item(new_definition_id(), new_game_id()) is False


async def test_page_query_is_not_scoped_to_a_viewer():
    pool = _pool_returning([], total=0)
    repository = CollectionsRepository(pool)

    await repository.list_definition_items_page(new_definition_id())

    assert [sql for sql, _ in pool.connections[0].executed if "le.identity_sub = cd.identity_sub" not in sql] == [], (
        "access resolves from the owner's sub, never a viewer's"
    )
