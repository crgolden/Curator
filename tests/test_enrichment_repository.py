"""Tests for EnrichmentRepository, using hand-written fake async psycopg_pool objects."""

from __future__ import annotations

from curator.enrichment.opencritic_matcher import OpenCriticGame
from curator.enrichment.repository import EnrichmentRepository, PsnCatalogCacheEntry


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
        self.fetchone_results = list(fetchone_results or [])
        self.fetchall_results = list(fetchall_results or [])

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


async def test_get_rawg_cache_returns_none_when_no_row():
    repo = EnrichmentRepository(FakePool(fetchone_results=[None]))

    assert await repo.get_rawg_cache("God of War") is None


async def test_get_rawg_cache_maps_row():
    pool = FakePool(fetchone_results=[("god of war", 123, {"id": 123})])
    repo = EnrichmentRepository(pool)

    entry = await repo.get_rawg_cache("God of War")

    assert entry is not None
    assert entry.rawg_game_id == 123
    assert entry.raw == {"id": 123}
    _sql, params = pool.connections[0].executed[0]
    assert params == ("god of war",)


async def test_save_rawg_cache_no_match_stores_null_raw():
    pool = FakePool()
    repo = EnrichmentRepository(pool)

    await repo.save_rawg_cache("Unknown Game", rawg_game_id=None, raw=None)

    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO rawg_cache" in sql
    assert params == ("unknown game", None, None)


async def test_get_all_opencritic_games_maps_rows():
    pool = FakePool(fetchall_results=[[(1, "Game A", 85, "Strong", 90)]])
    repo = EnrichmentRepository(pool)

    games = await repo.get_all_opencritic_games()

    assert games == [
        OpenCriticGame(oc_game_id=1, name="Game A", top_critic_score=85, tier="Strong", percent_recommended=90)
    ]


async def test_save_opencritic_games_upserts_each():
    pool = FakePool()
    repo = EnrichmentRepository(pool)
    games = [
        OpenCriticGame(oc_game_id=1, name="Game A", top_critic_score=85, tier="Strong", percent_recommended=90),
        OpenCriticGame(oc_game_id=2, name="Game B", top_critic_score=None, tier="", percent_recommended=None),
    ]

    await repo.save_opencritic_games(games)

    conn = pool.connections[0]
    assert len(conn.executed) == 2
    assert conn.executed[0][1] == (1, "Game A", 85, "Strong", 90)


async def test_get_psn_catalog_cache_maps_row():
    pool = FakePool(fetchone_results=[("p1", "c1", ["Action", "RPG"], 4.5, "Sony", "2020-01-01", "cover.png")])
    repo = EnrichmentRepository(pool)

    entry = await repo.get_psn_catalog_cache("p1")

    assert entry == PsnCatalogCacheEntry(
        product_id="p1",
        concept_id="c1",
        genres=("Action", "RPG"),
        star_rating=4.5,
        publisher="Sony",
        release_date="2020-01-01",
        cover_image_url="cover.png",
    )


async def test_get_psn_catalog_cache_returns_none_when_absent():
    repo = EnrichmentRepository(FakePool(fetchone_results=[None]))

    assert await repo.get_psn_catalog_cache("missing") is None


async def test_save_psn_catalog_cache_executes_upsert():
    pool = FakePool()
    repo = EnrichmentRepository(pool)
    entry = PsnCatalogCacheEntry(
        product_id="p1",
        concept_id="c1",
        genres=("Action",),
        star_rating=4.0,
        publisher="Sony",
        release_date="2020-01-01",
        cover_image_url=None,
    )

    await repo.save_psn_catalog_cache(entry)

    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO psn_catalog_cache" in sql
    assert params is not None
    assert params[0] == "p1"
    assert params[2] == ["Action"]


async def test_get_active_genres_maps_rows():
    pool = FakePool(fetchall_results=[[("id-1", "Shooter", 0), ("id-2", "RPG", 1)]])
    repo = EnrichmentRepository(pool)

    genres = await repo.get_active_genres()

    assert genres == [("id-1", "Shooter", 0), ("id-2", "RPG", 1)]


async def test_flag_data_quality_executes_insert():
    pool = FakePool()
    repo = EnrichmentRepository(pool)

    await repo.flag_data_quality("same_title_different_product_id", {"title": "Game"})

    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO data_quality_flags" in sql
    assert params is not None
    assert params[0] == "same_title_different_product_id"


async def test_save_game_enrichment_executes_upsert():
    from curator.enrichment.enrichment_service import EnrichmentResult

    pool = FakePool()
    repo = EnrichmentRepository(pool)
    result = EnrichmentResult(
        genre="Action",
        subgenre="Adventure",
        release_year=2020,
        developer="Dev",
        publisher="Pub",
        esrb="M",
        multiplayer=True,
        critical_score=85.0,
        oc_score=80.0,
        oc_tier="Strong",
        oc_percent_recommended=90.0,
        score_source="RAWG + OC",
        aaa_tier="AAA",
    )

    await repo.save_game_enrichment("game-1", "genre-id-1", "subgenre-id-1", result)

    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO game_enrichment" in sql
    assert params is not None
    assert params[0] == "game-1"
    assert params[1] == "genre-id-1"
    assert params[2] == "subgenre-id-1"
