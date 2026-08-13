"""Tests for CollectionsRepository, using hand-written fake async psycopg_pool objects."""

from __future__ import annotations

from datetime import datetime, timezone

from curator.catalog.cover_art import SQUARE_COVER_ART_SQL
from curator.collections.collection_spec import CollectionSpec
from curator.collections.filter_predicate import GenreIn
from curator.collections.game_candidate import GameCandidate
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


async def test_list_user_consoles_maps_rows_and_computes_effective_capacity():
    pool = FakePool(
        fetchall_results=[[("console-1", "My PS5", "PS5", 3997.0, 200.0, ["RPG"], 0, "PS5 Digital Edition")]]
    )
    repo = CollectionsRepository(pool)

    consoles = await repo.list_user_consoles("sub-1")

    assert len(consoles) == 1
    assert consoles[0].console_id == "console-1"
    assert consoles[0].effective_capacity_gb == 3797.0
    assert consoles[0].routing_genres == ("RPG",)
    assert consoles[0].model == "PS5 Digital Edition"


async def test_list_candidates_no_platform_filter():
    pool = FakePool(
        fetchall_results=[
            [
                (
                    "game-1",
                    "God of War",
                    "Action",
                    "AAA",
                    "God of War",
                    90.0,
                    85.0,
                    None,
                    False,
                    None,
                    "NPWR12345_00",
                    63,
                )
            ]
        ]
    )
    repo = CollectionsRepository(pool)

    candidates = await repo.list_candidates("sub-1")

    assert len(candidates) == 1
    assert candidates[0].game_id == "game-1"
    assert candidates[0].critical_score == 90.0
    assert candidates[0].np_communication_id == "NPWR12345_00"
    assert candidates[0].percent_completed == 63
    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "native_ps5 = true" not in sql
    assert "ps4_eligible = true" not in sql
    assert params == ("sub-1",)


async def test_list_candidates_applies_the_completion_floor_in_sql():
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates("sub-1", min_percent_completed=80)

    sql, params = pool.connections[0].executed[0]
    assert "le.trophy_percent_completed >= %s" in sql
    assert params == ("sub-1", 80)


async def test_completion_floor_is_skipped_for_a_user_with_no_stored_progress():
    """A floor must not empty a collection for a user who has never had a trophy refresh.

    Their rows are all NULL, so a plain ``>=`` would exclude everything -- for a reason that has nothing
    to do with the collection. The NOT EXISTS escape hatch is the SQL equivalent of the
    ``completion_available`` guard the in-memory filter uses.
    """
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates("sub-1", min_percent_completed=80)

    sql, _params = pool.connections[0].executed[0]
    assert "NOT EXISTS" in sql
    assert "probe.trophy_percent_completed IS NOT NULL" in sql


async def test_list_candidates_omits_the_completion_clause_when_no_floor_is_set():
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates("sub-1")

    sql, params = pool.connections[0].executed[0]
    assert "le.trophy_percent_completed >= %s" not in sql
    assert "NOT EXISTS (\n                        SELECT 1 FROM library_entries probe" not in sql
    assert params == ("sub-1",)


async def test_list_candidates_filters_by_ps5_platform():
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates("sub-1", platform="PS5")

    sql, _params = pool.connections[0].executed[0]
    assert "le.native_ps5 = true" in sql


async def test_list_candidates_filters_by_ps4_platform():
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates("sub-1", platform="PS4")

    sql, _params = pool.connections[0].executed[0]
    assert "le.ps4_eligible = true" in sql


async def test_list_candidates_excludes_inactive_entitlements_by_default():
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates("sub-1")

    sql, _params = pool.connections[0].executed[0]
    assert "le.is_active = true" in sql


async def test_list_candidates_include_inactive_drops_the_active_clause():
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates("sub-1", include_inactive=True)

    sql, _params = pool.connections[0].executed[0]
    assert "is_active" not in sql


async def test_list_candidates_orders_deterministically():
    """Bin-packing (curator.collections.capacity_fill_strategy) is size-order-sensitive, and Python's
    sorted() is stable -- so ties at equal composite score need a deterministic starting order, or the
    same collection could pack differently between two runs. See sort_order's own module docstring."""
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates("sub-1")

    sql, _params = pool.connections[0].executed[0]
    assert "ORDER BY g.canonical_title, g.game_id" in sql


async def test_list_candidates_omits_the_installed_elsewhere_clause_when_not_asked():
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates("sub-1")

    sql, params = pool.connections[0].executed[0]
    assert "console_installs" not in sql
    assert params == ("sub-1",)


async def test_list_candidates_excludes_games_installed_on_the_given_consoles():
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates("sub-1", exclude_installed_on=("c1", "c2"))

    sql, params = pool.connections[0].executed[0]
    assert "console_installs" in sql
    assert "ci.installed = true" in sql
    assert "uc.identity_sub = %s" in sql
    assert params == ("sub-1", "sub-1", ["c1", "c2"])


async def test_save_definition_returns_new_id_and_serializes_genre_filter():
    pool = FakePool(fetchone_results=[("def-1",)])
    repo = CollectionsRepository(pool)
    spec = CollectionSpec(kind="filter_list", genre_filter=("RPG", "Action"), min_score=80.0)

    definition_id = await repo.save_definition("sub-1", "My RPGs", spec)

    assert definition_id == "def-1"
    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO collection_definitions" in sql
    assert params is not None
    assert params[:-2] == (
        "sub-1",
        "My RPGs",
        None,
        "filter_list",
        None,
        ["RPG", "Action"],
        80.0,
        None,
        None,
        False,
        None,
        None,
    )
    assert isinstance(params[-2], str)
    assert len(params[-2]) > 0
    assert params[-1] == []


async def test_save_definition_persists_min_percent_completed():
    pool = FakePool(fetchone_results=[("def-1",)])
    repo = CollectionsRepository(pool)
    spec = CollectionSpec(kind="filter_list", min_percent_completed=75)

    await repo.save_definition("sub-1", "Nearly Done", spec)

    _sql, params = pool.connections[0].executed[0]
    assert params is not None
    assert params[:-2] == ("sub-1", "Nearly Done", None, "filter_list", None, [], None, None, None, False, 75, None)
    assert isinstance(params[-2], str)
    assert len(params[-2]) > 0


async def test_save_definition_persists_exclude_installed_on():
    pool = FakePool(fetchone_results=[("def-1",)])
    repo = CollectionsRepository(pool)
    spec = CollectionSpec(kind="filter_list", exclude_installed_on=("c1", "c2"))

    await repo.save_definition("sub-1", "Not on my PS5", spec)

    _sql, params = pool.connections[0].executed[0]
    assert params is not None
    assert params[-1] == ["c1", "c2"]


async def test_save_definition_serializes_filter_predicate_as_json():
    pool = FakePool(fetchone_results=[("def-1",)])
    repo = CollectionsRepository(pool)
    spec = CollectionSpec(kind="filter_list", filter_predicate=GenreIn(values=("RPG", "Action")))

    await repo.save_definition("sub-1", "My RPGs", spec)

    _sql, params = pool.connections[0].executed[0]
    assert params is not None
    assert params[-3] == '{"op": "genre_in", "values": ["RPG", "Action"]}'


async def test_save_definition_leaves_filter_predicate_null_when_not_supplied():
    pool = FakePool(fetchone_results=[("def-1",)])
    repo = CollectionsRepository(pool)

    await repo.save_definition("sub-1", "My RPGs", CollectionSpec(kind="filter_list"))

    _sql, params = pool.connections[0].executed[0]
    assert params is not None
    assert params[-3] is None


async def test_save_definition_stores_membership_ranked_by_position():
    pool = FakePool(fetchone_results=[("def-1",)])
    repo = CollectionsRepository(pool)

    await repo.save_definition(
        "sub-1", "My RPGs", CollectionSpec(kind="filter_list"), description="Best of", game_ids=("g2", "g1")
    )

    executed = pool.connections[0].executed
    assert executed[0][1] is not None
    assert executed[0][1][2] == "Best of"
    assert "INSERT INTO collection_definition_items" in executed[1][0]
    assert [call[1] for call in executed[1:]] == [("def-1", "g2", 1), ("def-1", "g1", 2)]


async def test_save_definition_drops_duplicate_game_ids():
    pool = FakePool(fetchone_results=[("def-1",)])
    repo = CollectionsRepository(pool)

    await repo.save_definition("sub-1", "Dupes", CollectionSpec(kind="filter_list"), game_ids=("g1", "g2", "g1"))

    item_inserts = [call for call in pool.connections[0].executed if "collection_definition_items" in call[0]]
    assert [call[1] for call in item_inserts] == [("def-1", "g1", 1), ("def-1", "g2", 2)]


async def test_list_definitions_maps_rows():
    pool = FakePool(
        fetchall_results=[
            [
                (
                    "def-1",
                    "sub-1",
                    "My RPGs",
                    "filter_list",
                    None,
                    ["RPG"],
                    80.0,
                    None,
                    None,
                    "Best of",
                    True,
                    60,
                    None,
                    "unlisted",
                    "abc123",
                    3,
                    ["c1"],
                )
            ],
        ]
    )
    repo = CollectionsRepository(pool)

    definitions = await repo.list_definitions("sub-1")

    assert len(definitions) == 1
    assert definitions[0].definition_id == "def-1"
    assert definitions[0].genre_filter == ("RPG",)
    assert definitions[0].visibility == "unlisted"
    assert definitions[0].share_slug == "abc123"
    assert definitions[0].item_count == 3
    assert definitions[0].min_score == 80.0
    assert definitions[0].description == "Best of"
    assert definitions[0].include_inactive is True
    assert definitions[0].min_percent_completed == 60
    assert definitions[0].filter_predicate is None
    assert definitions[0].exclude_installed_on == ("c1",)
    assert definitions[0].to_spec().include_inactive is True
    assert definitions[0].to_spec().min_percent_completed == 60
    assert definitions[0].to_spec().exclude_installed_on == ("c1",)


async def test_list_definitions_parses_a_stored_filter_predicate():
    pool = FakePool(
        fetchall_results=[
            [
                (
                    "def-1",
                    "sub-1",
                    "Criterion-ish",
                    "filter_list",
                    None,
                    [],
                    None,
                    None,
                    None,
                    None,
                    False,
                    None,
                    {"op": "genre_in", "values": ["RPG"]},
                    "private",
                    None,
                    0,
                    [],
                )
            ],
        ]
    )
    repo = CollectionsRepository(pool)

    definitions = await repo.list_definitions("sub-1")

    assert definitions[0].filter_predicate == GenreIn(values=("RPG",))
    assert definitions[0].to_spec().filter_predicate == GenreIn(values=("RPG",))


async def test_get_definition_scopes_to_identity_sub():
    pool = FakePool(
        fetchone_results=[
            (
                "def-1",
                "sub-1",
                "My RPGs",
                "filter_list",
                None,
                ["RPG"],
                80.0,
                None,
                None,
                None,
                False,
                None,
                None,
                "private",
                "xyz789",
                0,
                [],
            )
        ]
    )
    repo = CollectionsRepository(pool)

    definition = await repo.get_definition("sub-1", "def-1")

    assert definition is not None
    assert definition.name == "My RPGs"
    assert definition.min_percent_completed is None
    _sql, params = pool.connections[0].executed[0]
    assert params == ("sub-1", "def-1")


async def test_existing_game_ids_casts_to_uuid_array():
    pool = FakePool(fetchall_results=[[("g1",)]])
    repo = CollectionsRepository(pool)

    known = await repo.existing_game_ids(("g1", "g2"))

    assert known == {"g1"}
    sql, params = pool.connections[0].executed[0]
    assert "ANY(%s::uuid[])" in sql
    assert params == (["g1", "g2"],)


async def test_existing_game_ids_short_circuits_on_empty_input():
    pool = FakePool()
    repo = CollectionsRepository(pool)

    assert await repo.existing_game_ids(()) == set()
    assert pool.connections == []


async def test_list_definition_items_takes_no_identity_sub_and_orders_by_rank():
    row = ("g1", 1, "God of War", "God of War", "Action", "AAA", 94.0, 92.0, 4.5, "a.png", True)
    pool = FakePool(fetchall_results=[[row]])
    repo = CollectionsRepository(pool)

    items = await repo.list_definition_items("def-1")

    assert len(items) == 1
    assert items[0].rank == 1
    assert items[0].title == "God of War"
    assert items[0].cover_image_url == "a.png"
    assert items[0].owner_has_access is True
    sql, params = pool.connections[0].executed[0]
    assert params == ("def-1",)
    assert "ORDER BY cdi.rank" in sql
    assert "cd.identity_sub" in sql


async def test_list_definition_items_reports_a_game_the_owner_lost_access_to():
    row = ("g1", 1, "Lapsed Title", None, None, None, None, None, None, None, False)
    pool = FakePool(fetchall_results=[[row]])
    repo = CollectionsRepository(pool)

    items = await repo.list_definition_items("def-1")

    assert items[0].owner_has_access is False


async def test_list_definition_items_reads_entitlement_artwork():
    """Collection items carry the square entitlement icon, identity-free so a collection of
    freshly-imported games renders for a viewer who is not the importer."""
    row = ("g1", 1, "God of War", None, None, None, None, None, None, "entitlement.png", True)
    pool = FakePool(fetchall_results=[[row]])
    repo = CollectionsRepository(pool)

    items = await repo.list_definition_items("def-1")

    assert items[0].cover_image_url == "entitlement.png"
    sql, _params = pool.connections[0].executed[0]
    assert "entitlement_snapshots" in sql


async def test_list_definition_items_never_serves_storefront_hero_art():
    """``psn_catalog_cache`` holds 16:9 key art, which would render this list at a different shape from
    every other surface showing the same games."""
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_definition_items("def-1")

    sql, _params = pool.connections[0].executed[0]
    assert "psn_catalog_cache" not in sql
    assert "pcc." not in sql


async def test_list_definition_items_uses_the_shared_cover_art_expression():
    """Every cover-returning query interpolates the same constant, so none can drift into a second
    artwork policy. Nothing in this FakePool suite executes against a real schema, so a column that
    exists in no table would otherwise only surface as an UndefinedColumn 500 at runtime."""
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_definition_items("def-1")

    sql, _params = pool.connections[0].executed[0]
    assert SQUARE_COVER_ART_SQL in sql


async def test_update_definition_sets_updated_at_explicitly():
    pool = FakePool()
    repo = CollectionsRepository(pool)

    await repo.update_definition("def-1", name="Renamed", description=None)

    sql, params = pool.connections[0].executed[0]
    assert "updated_at = now()" in sql
    assert params == ("Renamed", None, "def-1")


async def test_update_definition_leaves_membership_alone_when_game_ids_is_none():
    pool = FakePool()
    repo = CollectionsRepository(pool)

    await repo.update_definition("def-1", name="Renamed", description=None)

    assert len(pool.connections[0].executed) == 1


async def test_update_definition_replaces_membership_in_the_same_transaction():
    pool = FakePool()
    repo = CollectionsRepository(pool)

    await repo.update_definition("def-1", name="Renamed", description=None, game_ids=("g3", "g4"))

    assert len(pool.connections) == 1
    executed = pool.connections[0].executed
    assert "UPDATE collection_definitions" in executed[0][0]
    assert "DELETE FROM collection_definition_items" in executed[1][0]
    assert [call[1] for call in executed[2:]] == [("def-1", "g3", 1), ("def-1", "g4", 2)]


async def test_delete_definition_reports_whether_a_row_went():
    pool = FakePool(rowcount=1)
    repo = CollectionsRepository(pool)

    assert await repo.delete_definition("sub-1", "def-1") is True
    _sql, params = pool.connections[0].executed[0]
    assert params == ("sub-1", "def-1")


async def test_delete_definition_returns_false_when_not_the_callers():
    pool = FakePool(rowcount=0)
    repo = CollectionsRepository(pool)

    assert await repo.delete_definition("sub-1", "def-b") is False


async def test_get_definition_returns_none_when_not_found():
    pool = FakePool(fetchone_results=[None])
    repo = CollectionsRepository(pool)

    definition = await repo.get_definition("sub-1", "unknown")

    assert definition is None


async def test_save_run_writes_run_and_items():
    pool = FakePool(fetchone_results=[("run-1",)])
    repo = CollectionsRepository(pool)
    included = [
        GameCandidate(
            game_id="g1",
            title="Game 1",
            genre="RPG",
            aaa_tier="AAA",
            franchise="",
            composite_score=90.0,
            rank_score=3,
            size_gb=50.0,
        )
    ]
    excluded = [
        GameCandidate(
            game_id="g2",
            title="Game 2",
            genre="Sports",
            aaa_tier="AA",
            franchise="",
            composite_score=40.0,
            rank_score=0,
            size_gb=20.0,
        )
    ]

    run_id = await repo.save_run("sub-1", None, {"kind": "capacity_fill"}, included, excluded)

    assert run_id == "run-1"
    conn = pool.connections[0]
    assert "INSERT INTO collection_runs" in conn.executed[0][0]
    assert "INSERT INTO collection_items" in conn.executed[1][0]
    included_params = conn.executed[1][1]
    assert included_params is not None
    assert included_params[:3] == ("run-1", "g1", 1)
    assert "INSERT INTO collection_items" in conn.executed[2][0]
    excluded_params = conn.executed[2][1]
    assert excluded_params is not None
    assert excluded_params[:2] == ("run-1", "g2")


async def test_list_measured_sizes_maps_rows():
    recorded_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pool = FakePool(fetchall_results=[[("g1", "PS5", 42.5, "sub-a", recorded_at)]])
    repo = CollectionsRepository(pool)

    sizes = await repo.list_measured_sizes("g1")

    assert len(sizes) == 1
    assert sizes[0].game_id == "g1"
    assert sizes[0].platform == "PS5"
    assert sizes[0].size_gb == 42.5
    assert sizes[0].recorded_by == "sub-a"
    assert sizes[0].recorded_at == recorded_at


async def test_list_measured_sizes_reports_no_contributor_after_recorded_by_is_set_null():
    recorded_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pool = FakePool(fetchall_results=[[("g1", "PS5", 42.5, None, recorded_at)]])
    repo = CollectionsRepository(pool)

    sizes = await repo.list_measured_sizes("g1")

    assert sizes[0].recorded_by is None


async def test_upsert_measured_size_upserts_on_game_and_platform():
    recorded_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    pool = FakePool(fetchone_results=[(recorded_at,)])
    repo = CollectionsRepository(pool)

    measured_size = await repo.upsert_measured_size("g1", "PS5", 42.5, "sub-a")

    assert measured_size.game_id == "g1"
    assert measured_size.platform == "PS5"
    assert measured_size.size_gb == 42.5
    assert measured_size.recorded_by == "sub-a"
    assert measured_size.recorded_at == recorded_at
    conn = pool.connections[0]
    assert "ON CONFLICT (game_id, platform) DO UPDATE" in conn.executed[0][0]
    assert conn.executed[0][1] == ("g1", "PS5", 42.5, "sub-a")
