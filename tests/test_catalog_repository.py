"""Tests for CatalogRepository, using hand-written fake async psycopg_pool objects.

Unlike tests/test_repository.py's single-fetchone-per-connection fakes, CatalogRepository's
upsert_game()/record_pull() issue several sequential statements against the SAME connection (matching a
real psycopg_pool.AsyncConnectionPool transaction), so FakeConnection here queues fetchone/fetchall
results consumed in call order instead of returning one fixed value.
"""

from __future__ import annotations

from curator.catalog.cover_art import SQUARE_COVER_ART_SQL
from curator.catalog.repository import CatalogRepository


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection

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
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.executed: list[tuple[str, tuple | None]] = []
        self.fetchone_results: list[tuple | None] = list(fetchone_results or [])
        self.fetchall_results: list[list[tuple]] = list(fetchall_results or [])

    def cursor(self):
        return FakeCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self._fetchone_results = fetchone_results or []
        self._fetchall_results = fetchall_results or []
        self.connections: list[FakeConnection] = []

    def connection(self):
        conn = FakeConnection(
            fetchone_results=list(self._fetchone_results), fetchall_results=list(self._fetchall_results)
        )
        self.connections.append(conn)
        return conn


async def test_list_genres_flattens_rows_to_names():
    pool = FakePool(fetchall_results=[[("Shooter",), ("RPG",), ("Adventure",)]])
    repo = CatalogRepository(pool)

    genres = await repo.list_genres()

    assert genres == ["Shooter", "RPG", "Adventure"]


async def test_list_games_uses_the_shared_cover_art_expression():
    """Browse and the detail page show the same game, so a second artwork policy here would give it two
    different shapes depending on which page the reader arrived at."""
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = CatalogRepository(pool)

    await repo.list_games()

    select_sql, _params = pool.connections[0].executed[1]
    assert SQUARE_COVER_ART_SQL in select_sql


async def test_get_game_uses_the_shared_cover_art_expression():
    pool = FakePool(fetchone_results=[None])
    repo = CatalogRepository(pool)

    await repo.get_game("game-1")

    select_sql, _params = pool.connections[0].executed[0]
    assert SQUARE_COVER_ART_SQL in select_sql


async def test_cover_art_queries_never_read_storefront_hero_art():
    """``psn_catalog_cache.cover_image_url`` is 16:9 key art and covers a minority of titles; it stays
    available for ``store_product_id`` but must not answer a cover-art lookup."""
    pool = FakePool(fetchone_results=[(0,)], fetchall_results=[[]])
    repo = CatalogRepository(pool)

    await repo.list_games()

    select_sql, _params = pool.connections[0].executed[1]
    assert "pcc.cover_image_url" not in select_sql
    assert "pcc.store_product_id" in select_sql


async def test_list_genres_offers_only_genres_an_enriched_game_already_carries():
    pool = FakePool(fetchall_results=[[("Shooter",)]])
    repo = CatalogRepository(pool)

    await repo.list_genres()

    sql, _params = pool.connections[0].executed[0]
    assert "JOIN game_enrichment ge ON ge.genre_id = gen.genre_id" in sql
    assert "WHERE gen.active = true" in sql
    assert "ORDER BY gen.priority" in sql
