"""Tests for LibraryRepository, using hand-written fake async psycopg_pool objects."""

from __future__ import annotations

from typing import get_args
from uuid import uuid4

import pytest

from curator.catalog.cover_art import SQUARE_COVER_ART_SQL
from curator.library.repository import (
    _OWNED_PLATFORMS_SQL,
    _SORT_COLUMNS,
    HIDDEN_ONLY,
    HIDDEN_REASON,
    LIBRARY_SOURCE_PSN,
    TROPHY_MATCHED,
    TROPHY_NOT_ATTEMPTED,
    TROPHY_UNMATCHED,
    LibraryRepository,
    LibrarySortField,
)
from curator.psn.title_platform import PS4, PS5
from test_values import (
    lowercase_token,
    new_cover_image_url,
    new_game_id,
    new_game_title,
    new_genre_name,
    new_identity_sub,
    new_np_communication_id,
    new_percent_completed,
    new_positive_count,
    new_psn_rating,
    new_review_score,
    new_store_product_id,
    new_utc_instant,
)


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

    await repo.upsert_manual_entry(new_identity_sub(), new_game_id(), platforms=(PS5,), owned_edition=None)

    executed = pool.connections[0].executed
    assert not any("library_entry_platforms" in sql for sql, _ in executed)


async def test_list_entries_with_enrichment_maps_rows_and_total():
    identity_sub = new_identity_sub()
    expected_total = new_positive_count()
    first_game_id = new_game_id()
    second_game_id = new_game_id()
    genre = new_genre_name()
    rawg_rating = new_review_score()
    opencritic_rating = new_review_score()
    psn_rating = new_psn_rating()
    product_id = new_store_product_id()
    np_communication_id = new_np_communication_id()
    percent_completed = new_percent_completed()
    cover_image_url = new_cover_image_url()
    second_source = lowercase_token()
    pool = FakePool(
        fetchone_results=[(expected_total,)],
        fetchall_results=[
            [
                (
                    first_game_id,
                    new_game_title(),
                    genre,
                    rawg_rating,
                    opencritic_rating,
                    psn_rating,
                    product_id,
                    True,
                    True,
                    False,
                    True,
                    np_communication_id,
                    percent_completed,
                    LIBRARY_SOURCE_PSN,
                    cover_image_url,
                    [PS5, PS4],
                    new_utc_instant(),
                ),
                (
                    second_game_id,
                    new_game_title(),
                    None,
                    None,
                    None,
                    None,
                    None,
                    False,
                    False,
                    True,
                    False,
                    None,
                    None,
                    second_source,
                    None,
                    [],
                    None,
                ),
            ]
        ],
    )
    repo = LibraryRepository(pool)

    games, total = await repo.list_entries_with_enrichment(identity_sub)

    assert total == expected_total
    assert [game.game_id for game in games] == [first_game_id, second_game_id]
    assert games[0].trophy_match == TROPHY_MATCHED
    assert games[1].trophy_match == TROPHY_NOT_ATTEMPTED
    assert games[0].genre == genre
    assert games[0].rawg_rating == rawg_rating
    assert games[0].opencritic_rating == opencritic_rating
    assert games[0].psn_rating == psn_rating
    assert games[0].psn_product_id == product_id
    assert games[0].rawg_enriched is True
    assert games[0].opencritic_enriched is True
    assert games[0].psn_enriched is False
    assert games[1].psn_enriched is True
    assert games[0].is_active is True
    assert games[0].np_communication_id == np_communication_id

    assert games[0].percent_completed == percent_completed
    assert games[0].cover_image_url == cover_image_url
    assert games[1].percent_completed is None
    assert games[1].genre is None
    assert games[1].cover_image_url is None
    assert games[0].source == LIBRARY_SOURCE_PSN
    assert games[1].source == second_source, "provenance must survive the row mapping so the UI can mark it"
    assert games[0].platforms == (PS5, PS4)
    assert games[1].platforms == ()

    assert games[1].is_active is False
    assert games[1].np_communication_id is None


async def test_list_entries_with_enrichment_reports_psn_enriched_from_the_column_not_the_star_rating():
    """``psn_enriched`` records that a PS Store concept was obtained, from cache or fresh. A concept that
    filled genre, publisher and release date but carried no star rating leaves ``psn_rating`` NULL and is
    still real enrichment, so a ``psn_rating IS NOT NULL`` proxy under-reports it -- the defect
    ``0049_game_enrichment_psn_enriched.sql`` exists to prevent. Functions pins the write side of the same
    pairing in ``EnrichmentRepositoryTests.SaveGameEnrichmentAsync_WritesPsnEnrichedFromTheProvenanceSignal
    _NotFromWhetherAStarRatingArrived``."""
    identity_sub = uuid4().hex
    game_id = uuid4().hex
    title = uuid4().hex
    concept_without_a_star_rating = (
        game_id,
        title,
        None,
        None,
        None,
        None,
        None,
        False,
        False,
        True,
        True,
        None,
        None,
        LIBRARY_SOURCE_PSN,
        None,
        None,
        new_utc_instant(),
    )
    pool = FakePool(fetchone_results=[(1,)], fetchall_results=[[concept_without_a_star_rating]])
    repo = LibraryRepository(pool)

    games, _total = await repo.list_entries_with_enrichment(identity_sub)

    select_sql, _ = pool.connections[0].executed[1]
    assert "COALESCE(ge.psn_enriched, false)" in select_sql
    assert games[0].psn_rating is None
    assert games[0].psn_enriched is True
    assert games[0].trophy_match == TROPHY_UNMATCHED, "a refresh tried and matched nothing"


async def test_list_entries_leaves_hidden_games_out_by_default():
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    await repo.list_entries_with_enrichment(str(uuid4()))

    count_sql, _ = pool.connections[0].executed[0]
    select_sql, _ = pool.connections[0].executed[1]
    assert "NOT EXISTS (" in count_sql
    assert "FROM library_exclusions lx" in count_sql
    assert "NOT EXISTS (" in select_sql
    assert "FROM library_exclusions lx" in select_sql


async def test_list_entries_hidden_only_lists_nothing_but_hidden_games():
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    await repo.list_entries_with_enrichment(str(uuid4()), hidden=HIDDEN_ONLY)

    select_sql, _ = pool.connections[0].executed[1]
    assert "FROM library_exclusions lx" in select_sql
    assert "NOT EXISTS (\n                SELECT 1 FROM library_exclusions" not in select_sql


async def test_hiding_a_game_the_caller_holds_writes_a_keyed_exclusion():
    identity_sub, game_id = str(uuid4()), str(uuid4())
    pool = FakePool(fetchone_results=[(1,)])
    repo = LibraryRepository(pool)

    assert await repo.hide_entry(identity_sub, game_id) is True
    insert_sql, params = pool.connections[0].executed[1]
    assert "INSERT INTO library_exclusions" in insert_sql
    assert "ON CONFLICT (identity_sub, game_id) DO NOTHING" in insert_sql
    assert params == (identity_sub, game_id, HIDDEN_REASON, identity_sub)


async def test_hiding_a_game_the_caller_does_not_hold_writes_nothing():
    pool = FakePool(fetchone_results=[None])
    repo = LibraryRepository(pool)

    assert await repo.hide_entry(str(uuid4()), str(uuid4())) is False
    assert len(pool.connections[0].executed) == 1


async def test_unhiding_deletes_the_exclusion():
    identity_sub, game_id = str(uuid4()), str(uuid4())
    pool = FakePool()
    repo = LibraryRepository(pool)

    await repo.unhide_entry(identity_sub, game_id)

    sql, params = pool.connections[0].executed[0]
    assert sql.startswith("DELETE FROM library_exclusions")
    assert params == (identity_sub, game_id)


async def test_has_trophy_progress_probes_the_fetched_at_column():
    identity_sub = str(uuid4())
    pool = FakePool(fetchone_results=[(False,)])
    repo = LibraryRepository(pool)

    assert await repo.has_trophy_progress(identity_sub) is False
    sql, params = pool.connections[0].executed[0]
    assert "trophy_progress_fetched_at IS NOT NULL" in sql
    assert params == (identity_sub,)


async def test_list_entries_with_enrichment_builds_search_and_genre_conditions():
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    identity_sub = new_identity_sub()
    search = lowercase_token()
    genre = new_genre_name()

    await repo.list_entries_with_enrichment(identity_sub, search=search, genre=genre)

    count_sql, count_params = pool.connections[0].executed[0]
    assert "ILIKE" in count_sql
    assert "gen.name = %s" in count_sql
    assert count_params == (identity_sub, f"%{search}%", genre)

    select_sql, select_params = pool.connections[0].executed[1]
    assert "ILIKE" in select_sql
    assert select_params == (identity_sub, f"%{search}%", genre, 20, 0)


async def test_list_entries_with_enrichment_rejects_unknown_sort_field():
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    with pytest.raises(KeyError):
        await repo.list_entries_with_enrichment(new_identity_sub(), sort=lowercase_token())


def test_the_sort_allow_list_covers_exactly_the_declared_sort_fields():
    assert set(_SORT_COLUMNS) == set(get_args(LibrarySortField))


@pytest.mark.parametrize("sort", get_args(LibrarySortField))
async def test_list_entries_with_enrichment_orders_by_sort_column_nulls_last(sort):
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    await repo.list_entries_with_enrichment(new_identity_sub(), sort=sort, sort_dir="desc")

    select_sql, _ = pool.connections[0].executed[1]
    assert f"ORDER BY {_SORT_COLUMNS[sort]} DESC NULLS LAST, g.canonical_title ASC" in select_sql


async def test_list_entries_with_enrichment_reads_platforms_as_a_scalar_subquery():
    """A game owned on three platforms must stay one row. Joining ``library_entry_platforms`` into the
    shared ``FROM`` clause would multiply it and inflate the ``COUNT(*)`` taken over the same clause, so
    the page would claim more games than the user owns."""
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    await repo.list_entries_with_enrichment(new_identity_sub())

    count_sql, _ = pool.connections[0].executed[0]
    select_sql, _ = pool.connections[0].executed[1]
    assert "library_entry_platforms" not in count_sql, "the platform lookup must not reach the counted FROM clause"
    assert _OWNED_PLATFORMS_SQL in select_sql
    assert "ORDER BY p.sort_order" in select_sql


async def test_list_entries_with_enrichment_defaults_absent_platforms_to_empty():
    """``array_agg`` yields NULL over no rows; the caller must see an empty tuple, not ``None``."""
    row = (
        new_game_id(),
        new_game_title(),
        None,
        None,
        None,
        None,
        None,
        False,
        False,
        False,
        True,
        None,
        None,
        lowercase_token(),
        None,
        None,
        None,
    )
    pool = FakePool(fetchone_results=[(1,)], fetchall_results=[[row]])
    repo = LibraryRepository(pool)

    games, _total = await repo.list_entries_with_enrichment(new_identity_sub())

    assert games[0].platforms == ()


async def test_list_entries_with_enrichment_uses_the_shared_cover_art_expression():
    """The library table shows the same games as the catalog, so it must resolve artwork identically --
    ``psn_catalog_cache``'s 16:9 key art would render this table at a different shape from every other
    surface."""
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    await repo.list_entries_with_enrichment(new_identity_sub())

    select_sql, _ = pool.connections[0].executed[1]
    assert SQUARE_COVER_ART_SQL in select_sql
    assert "psn_catalog_cache" not in select_sql


async def test_list_entries_with_enrichment_applies_limit_and_offset():
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = LibraryRepository(pool)

    limit = new_positive_count()
    offset = new_positive_count()

    await repo.list_entries_with_enrichment(new_identity_sub(), limit=limit, offset=offset)

    _, select_params = pool.connections[0].executed[1]
    assert select_params is not None
    paging = (limit, offset)
    assert select_params[-len(paging) :] == paging


async def test_list_genres_returns_distinct_sorted_names():
    identity_sub = new_identity_sub()
    first_genre = new_genre_name()
    second_genre = new_genre_name()
    pool = FakePool(fetchall_results=[[(first_genre,), (second_genre,)]])
    repo = LibraryRepository(pool)

    genres = await repo.list_genres(identity_sub)

    assert genres == [first_genre, second_genre]
    sql, params = pool.connections[0].executed[0]
    assert "SELECT DISTINCT gen.name" in sql
    assert params == (identity_sub,)


async def test_list_genres_empty_when_no_rows():
    repo = LibraryRepository(FakePool(fetchall_results=[[]]))

    assert await repo.list_genres(new_identity_sub()) == []


async def test_clear_trophy_progress_erases_percentages_but_keeps_the_match():
    """Opting out of trophy harvesting must remove what was already collected, not just stop collecting.

    np_communication_id deliberately survives: it is an id-to-id mapping, not PSN activity data, and
    keeping it means re-enabling the preference doesn't have to re-run the whole matching pass.
    """
    pool = FakePool()
    repo = LibraryRepository(pool)

    identity_sub = new_identity_sub()

    await repo.clear_trophy_progress(identity_sub)

    sql, params = pool.connections[0].executed[0]
    assert "trophy_percent_completed = NULL" in sql
    assert "trophy_progress_fetched_at = NULL" in sql
    assert "np_communication_id" not in sql
    assert params == (identity_sub,)
