"""Tests for CatalogRepository, using hand-written fake async psycopg_pool objects.

Unlike tests/test_repository.py's single-fetchone-per-connection fakes, CatalogRepository's
upsert_game()/record_pull() issue several sequential statements against the SAME connection (matching a
real psycopg_pool.AsyncConnectionPool transaction), so FakeConnection here queues fetchone/fetchall
results consumed in call order instead of returning one fixed value.
"""

from __future__ import annotations

import uuid

from curator.catalog.cover_art import SQUARE_COVER_ART_SQL
from curator.catalog.repository import GAME_UPSERT_ADVISORY_LOCK_CLASS, CatalogRepository
from curator.psn.store_client import StoreProduct


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


class FakeTransaction:
    def __init__(self, connection):
        self._connection = connection

    async def __aenter__(self):
        self._connection.transactions_opened += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, fetchone_results=None, fetchall_results=None):
        self.executed: list[tuple[str, tuple | None]] = []
        self.fetchone_results: list[tuple | None] = list(fetchone_results or [])
        self.fetchall_results: list[list[tuple]] = list(fetchall_results or [])
        self.transactions_opened = 0

    def cursor(self):
        return FakeCursor(self)

    def transaction(self):
        return FakeTransaction(self)

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


async def test_backfill_keeps_the_whole_product_node_the_walk_already_paid_for():
    """_to_product projected six fields and dropped price, sortingOptions, skus and the sibling concepts
    collection -- out of a response POST /catalog/backfill already makes. Recovering any of them later
    costs a second full walk of a live, shifting collection, so the node is persisted whole."""
    node = {"id": "P1", "npTitleId": "CUSA00207_00", "price": {"basePrice": "$19.99", "isFree": False}}
    product = StoreProduct(
        product_id="P1",
        name="Bloodborne",
        platforms=("PS4",),
        np_title_id="CUSA00207_00",
        cover_image_url="cover.jpg",
        classification="Full Game",
        raw=node,
    )
    pool = FakePool(fetchone_results=[("game-1",)])
    repo = CatalogRepository(pool)

    await repo.backfill_store_products([product])

    cache_sql, cache_params = pool.connections[0].executed[1]
    assert "INSERT INTO psn_catalog_cache" in cache_sql
    assert cache_params is not None
    stored_raw = cache_params[4]
    assert stored_raw.obj == node, "the price payload must survive the walk that fetched it"


async def test_backfill_never_blanks_a_stored_payload_with_an_empty_one():
    product = StoreProduct(
        product_id="P1",
        name="Bloodborne",
        platforms=("PS4",),
        np_title_id="CUSA00207_00",
        cover_image_url=None,
        classification="Full Game",
    )
    pool = FakePool(fetchone_results=[("game-1",)])
    repo = CatalogRepository(pool)

    await repo.backfill_store_products([product])

    cache_sql, _params = pool.connections[0].executed[1]
    assert "raw = CASE WHEN EXCLUDED.raw = '{}'::jsonb THEN psn_catalog_cache.raw ELSE EXCLUDED.raw END" in cache_sql


async def test_title_id_for_game_prefers_the_most_recently_fetched_row():
    pool = FakePool(fetchone_results=[("BLUS30443_00",)])
    repo = CatalogRepository(pool)

    assert await repo.title_id_for_game("game-1") == "BLUS30443_00"

    sql, params = pool.connections[0].executed[0]
    assert "ORDER BY fetched_at DESC LIMIT 1" in sql, "a concept can carry several editions"
    assert params == ("game-1",)


async def test_title_id_for_game_returns_none_when_the_catalog_holds_no_psn_identifier():
    pool = FakePool(fetchone_results=[None])
    repo = CatalogRepository(pool)

    assert await repo.title_id_for_game("game-1") is None


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


async def test_store_id_lookup_reports_only_the_ids_that_resolved():
    resolved_id = uuid.uuid4().hex
    unresolved_id = uuid.uuid4().hex
    game_id = str(uuid.uuid4())
    pool = FakePool(fetchall_results=[[(resolved_id, game_id), (unresolved_id, None)]])
    repo = CatalogRepository(pool)

    resolved = await repo.game_ids_for_store_ids([resolved_id, unresolved_id])

    assert resolved == {resolved_id: game_id}


async def test_store_id_lookup_opens_no_connection_for_an_empty_page():
    pool = FakePool()
    repo = CatalogRepository(pool)

    assert await repo.game_ids_for_store_ids([]) == {}
    assert pool.connections == []


async def test_store_id_lookup_prefers_the_concept_id_over_either_product_id():
    """``game_concepts.concept_id`` is the primary key and is populated for every row; ``product_id`` is
    neither unique nor a safe merge key (``0001_initial.sql``), and ``store_product_id`` is sparse."""
    pool = FakePool(fetchall_results=[[]])
    repo = CatalogRepository(pool)

    await repo.game_ids_for_store_ids([uuid.uuid4().hex])

    sql, _params = pool.connections[0].executed[0]
    concept_match = sql.index("gc.concept_id = candidate.store_id")
    product_match = sql.index("gc.product_id = candidate.store_id")
    cache_match = sql.index("pcc.store_product_id = candidate.store_id")
    assert concept_match < product_match < cache_match


async def test_admitting_a_store_title_creates_the_game_its_concept_and_an_enrichment_row():
    concept_id = str(uuid.uuid4().int)[:6]
    product_id = f"UP1004-PPSA{uuid.uuid4().int % 100000:05d}_00-STANDARDEDITION0"
    expected_game_id = str(uuid.uuid4())
    title = f"Ghost of {uuid.uuid4().hex}"
    pool = FakePool(fetchone_results=[None, None, (expected_game_id,)])
    repo = CatalogRepository(pool)

    game_id, created = await repo.admit_store_game(concept_id=concept_id, name=title, product_id=product_id)

    assert (game_id, created) == (expected_game_id, True)
    statements = [sql for sql, _params in pool.connections[0].executed]
    assert "INSERT INTO games" in statements[3]
    assert "INSERT INTO game_concepts" in statements[4]
    assert "INSERT INTO game_enrichment" in statements[5]
    assert pool.connections[0].executed[4][1] == (concept_id, expected_game_id, product_id)


async def test_admitting_a_store_title_holds_an_advisory_lock_over_the_read_then_insert():
    """``games`` has an index on ``normalized_title`` but no unique constraint, so two concurrent admits
    of the same title would both see no row and both insert one."""
    title = f"Ghost of {uuid.uuid4().hex}"
    pool = FakePool(fetchone_results=[None, None, (str(uuid.uuid4()),)])
    repo = CatalogRepository(pool)

    await repo.admit_store_game(concept_id=str(uuid.uuid4().int)[:6], name=title)

    lock_sql, lock_params = pool.connections[0].executed[0]
    assert "pg_advisory_xact_lock" in lock_sql
    assert lock_params == (GAME_UPSERT_ADVISORY_LOCK_CLASS, title.lower())
    assert pool.connections[0].transactions_opened == 1


async def test_admitting_a_store_title_takes_the_same_lock_classid_functions_upsert_game_async_does():
    pool = FakePool(fetchone_results=[None, None, (str(uuid.uuid4()),)])
    repo = CatalogRepository(pool)

    await repo.admit_store_game(concept_id=str(uuid.uuid4().int)[:6], name=f"Ghost of {uuid.uuid4().hex}")

    lock_sql, lock_params = pool.connections[0].executed[0]
    assert lock_sql.count("%s") == 2, (
        "single-arg pg_advisory_xact_lock(hashtext(...)) is a different Postgres lock space from "
        "Functions' two-arg form and would never contend with UpsertGameAsync's lock"
    )
    assert lock_params is not None
    assert lock_params[0] == GAME_UPSERT_ADVISORY_LOCK_CLASS == 1, (
        "classid must equal Functions' CuratorAdvisoryLocks.GameUpsert (1) or the two repos take "
        "unrelated locks over the same title"
    )


async def test_admitting_a_title_whose_concept_is_already_mapped_writes_nothing_new():
    existing_game_id = str(uuid.uuid4())
    pool = FakePool(fetchone_results=[(existing_game_id,)])
    repo = CatalogRepository(pool)

    game_id, created = await repo.admit_store_game(
        concept_id=str(uuid.uuid4().int)[:6], name=f"Ghost of {uuid.uuid4().hex}"
    )

    assert (game_id, created) == (existing_game_id, False)
    statements = [sql for sql, _params in pool.connections[0].executed]
    assert not any("INSERT" in sql for sql in statements)


async def test_admitting_a_title_the_catalog_already_holds_by_name_reuses_that_game():
    existing_game_id = str(uuid.uuid4())
    pool = FakePool(fetchone_results=[None, (existing_game_id,)])
    repo = CatalogRepository(pool)

    game_id, created = await repo.admit_store_game(
        concept_id=str(uuid.uuid4().int)[:6], name=f"Ghost of {uuid.uuid4().hex}"
    )

    assert (game_id, created) == (existing_game_id, False)
    statements = [sql for sql, _params in pool.connections[0].executed]
    assert not any("INSERT INTO games" in sql for sql in statements)
    assert any("INSERT INTO game_concepts" in sql for sql in statements)


async def test_admitting_a_store_title_leaves_rawg_attempted_at_unset():
    """``rawg_attempted_at IS NULL`` is what ``EnrichmentRunProcessor`` unions into its candidate set via
    ``GetGameIdsNeverAskedOfRawgAsync``; stamping it here would strand the game unenriched forever."""
    pool = FakePool(fetchone_results=[None, None, (str(uuid.uuid4()),)])
    repo = CatalogRepository(pool)

    await repo.admit_store_game(concept_id=str(uuid.uuid4().int)[:6], name=f"Ghost of {uuid.uuid4().hex}")

    enrichment_sql, _params = pool.connections[0].executed[5]
    assert "rawg_attempted_at" not in enrichment_sql
    assert "ON CONFLICT (game_id) DO NOTHING" in enrichment_sql


async def test_admitting_a_store_title_never_writes_psn_catalog_cache():
    """A search hit carries no npTitleId, and that table is keyed by one. Deriving one by splitting the
    product id disagrees with the stored title id on 824 of 3045 ``entitlement_snapshots`` rows."""
    pool = FakePool(fetchone_results=[None, None, (str(uuid.uuid4()),)])
    repo = CatalogRepository(pool)

    await repo.admit_store_game(
        concept_id=str(uuid.uuid4().int)[:6],
        name=f"Ghost of {uuid.uuid4().hex}",
        product_id="UP1004-PPSA03420_00-GTAOSTANDALONE01",
    )

    statements = [sql for sql, _params in pool.connections[0].executed]
    assert not any("psn_catalog_cache" in sql for sql in statements)
