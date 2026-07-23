"""Tests for CatalogRepository, using hand-written fake async psycopg_pool objects.

Unlike tests/test_repository.py's single-fetchone-per-connection fakes, CatalogRepository's
upsert_game()/record_pull() issue several sequential statements against the SAME connection (matching a
real psycopg_pool.AsyncConnectionPool transaction), so FakeConnection here queues fetchone/fetchall
results consumed in call order instead of returning one fixed value.
"""

from __future__ import annotations

from curator.catalog.canonicalization_service import CanonicalGame, EntitlementSnapshot
from curator.catalog.franchise_assigner import FranchiseRule
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


async def test_list_exclusion_rules_maps_rows():
    pool = FakePool(fetchall_results=[[("id-1", "media_app", "Netflix")]])
    repo = CatalogRepository(pool)

    rules = await repo.list_exclusion_rules()

    assert len(rules) == 1
    assert rules[0].rule_id == "id-1"
    assert rules[0].rule_type == "media_app"
    assert rules[0].pattern == "Netflix"


async def test_list_franchise_rules_maps_rows():
    pool = FakePool(fetchall_results=[[("id-1", "god of war", "God of War", 0)]])
    repo = CatalogRepository(pool)

    rules = await repo.list_franchise_rules()

    assert rules[0].franchise == "God of War"
    assert rules[0].priority == 0


async def test_get_edition_ranks_builds_dict():
    pool = FakePool(fetchall_results=[[("director", 1), ("complete", 2)]])
    repo = CatalogRepository(pool)

    ranks = await repo.get_edition_ranks()

    assert ranks == {"director": 1, "complete": 2}


async def test_get_name_overrides_builds_dict():
    pool = FakePool(fetchall_results=[[("10005732", "Cities: Skylines Remastered")]])
    repo = CatalogRepository(pool)

    overrides = await repo.get_name_overrides()

    assert overrides == {"10005732": "Cities: Skylines Remastered"}


async def test_get_globally_excluded_concept_ids_builds_set():
    pool = FakePool(fetchall_results=[[("c1",), ("c2",)]])
    repo = CatalogRepository(pool)

    excluded = await repo.get_globally_excluded_concept_ids()

    assert excluded == {"c1", "c2"}


async def test_exclude_globally_executes_upsert():
    pool = FakePool()
    repo = CatalogRepository(pool)

    await repo.exclude_globally("c1", "confirmed duplicate")

    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO global_exclusions" in sql
    assert "ON CONFLICT (concept_id) DO UPDATE" in sql
    assert params == ("c1", "confirmed duplicate")


async def test_record_pull_writes_pull_and_snapshot_rows():
    pool = FakePool(fetchone_results=[("pull-1",)])
    repo = CatalogRepository(pool)
    snapshots = [
        EntitlementSnapshot(
            entitlement_id="e1",
            concept_id="c1",
            product_id="p1",
            title_id="t1",
            game_meta_name="Game",
            concept_meta_name="Game",
            title_meta_name="Game",
            package_type="PS4GD",
            active=True,
        )
    ]

    pull_id = await repo.record_pull("sub-1", "curator-live", snapshots)

    assert pull_id == "pull-1"
    conn = pool.connections[0]
    assert "INSERT INTO entitlement_pulls" in conn.executed[0][0]
    assert conn.executed[0][1] == ("sub-1", "curator-live", 1)
    assert "INSERT INTO entitlement_snapshots" in conn.executed[1][0]
    snapshot_params = conn.executed[1][1]
    assert snapshot_params is not None
    assert snapshot_params[0] == "pull-1"
    assert snapshot_params[1] == "e1"


async def test_upsert_game_matches_by_known_concept_id():
    pool = FakePool(fetchone_results=[("existing-game-id",)])
    repo = CatalogRepository(pool)
    game = CanonicalGame(
        canonical_title="God of War",
        native_ps5=False,
        ps4_eligible=True,
        franchise="God of War",
        product_id="p1",
        concept_ids=("c1",),
        winning_entitlement_id="e1",
    )

    game_id = await repo.upsert_game(game)

    assert game_id == "existing-game-id"
    conn = pool.connections[0]
    assert "SELECT game_id FROM game_concepts WHERE concept_id = ANY(%s)" in conn.executed[0][0]
    assert "UPDATE games SET canonical_title" in conn.executed[1][0]
    assert "INSERT INTO game_concepts" in conn.executed[2][0]
    assert conn.executed[2][1] == ("c1", "existing-game-id", "p1")


async def test_upsert_game_matches_by_normalized_title_when_no_concept_match():
    # concept lookup returns nothing, then title lookup finds an existing row.
    pool = FakePool(fetchone_results=[None, ("title-matched-id",)])
    repo = CatalogRepository(pool)
    game = CanonicalGame(
        canonical_title="God of War",
        native_ps5=False,
        ps4_eligible=True,
        franchise="God of War",
        product_id="p1",
        concept_ids=("c1",),
        winning_entitlement_id="e1",
    )

    game_id = await repo.upsert_game(game)

    assert game_id == "title-matched-id"


async def test_upsert_game_inserts_a_new_row_when_nothing_matches():
    pool = FakePool(fetchone_results=[None, None, ("new-game-id",)])
    repo = CatalogRepository(pool)
    game = CanonicalGame(
        canonical_title="Brand New Game",
        native_ps5=True,
        ps4_eligible=False,
        franchise="",
        product_id="p9",
        concept_ids=("c9",),
        winning_entitlement_id="e9",
    )

    game_id = await repo.upsert_game(game)

    assert game_id == "new-game-id"
    conn = pool.connections[0]
    assert "INSERT INTO games" in conn.executed[2][0]
    assert conn.executed[2][1] == ("Brand New Game", "brand new game", None)


async def test_upsert_game_with_no_concept_ids_skips_concept_lookup():
    pool = FakePool(fetchone_results=[("matched-by-title",)])
    repo = CatalogRepository(pool)
    game = CanonicalGame(
        canonical_title="Some Game",
        native_ps5=False,
        ps4_eligible=True,
        franchise="",
        product_id=None,
        concept_ids=(),
        winning_entitlement_id=None,
    )

    game_id = await repo.upsert_game(game)

    assert game_id == "matched-by-title"
    conn = pool.connections[0]
    assert "SELECT game_id FROM games WHERE normalized_title" in conn.executed[0][0]


async def test_list_all_game_ids_and_titles_maps_rows():
    pool = FakePool(fetchall_results=[[("id-1", "God of War"), ("id-2", "Horizon Zero Dawn")]])
    repo = CatalogRepository(pool)

    games = await repo.list_all_game_ids_and_titles()

    assert games == [("id-1", "God of War"), ("id-2", "Horizon Zero Dawn")]


async def test_reclassify_franchise_updates_only_changed_rows():
    # "Call of Duty: Black Ops 4" currently has no franchise, but a rule now matches it;
    # "God of War" already has the correct franchise assigned, so it should be left alone.
    pool = FakePool(
        fetchall_results=[
            [
                ("id-1", "Call of Duty: Black Ops 4", None),
                ("id-2", "God of War", "God of War"),
            ]
        ]
    )
    repo = CatalogRepository(pool)
    rules = [
        FranchiseRule(rule_id="r1", pattern="call of duty", franchise="Call of Duty", priority=0),
        FranchiseRule(rule_id="r2", pattern="god of war", franchise="God of War", priority=1),
    ]

    updated = await repo.reclassify_franchise(rules)

    assert updated == 1
    conn = pool.connections[0]
    update_calls = [call for call in conn.executed if call[0].startswith("UPDATE games")]
    assert len(update_calls) == 1
    assert update_calls[0][1] == ("Call of Duty", "id-1")


async def test_reclassify_franchise_returns_zero_when_nothing_changes():
    pool = FakePool(fetchall_results=[[("id-1", "Unmatched Game", None)]])
    repo = CatalogRepository(pool)

    updated = await repo.reclassify_franchise([])

    assert updated == 0
    conn = pool.connections[0]
    assert not any(call[0].startswith("UPDATE games") for call in conn.executed)
