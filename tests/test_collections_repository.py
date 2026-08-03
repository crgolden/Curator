"""Tests for CollectionsRepository, using hand-written fake async psycopg_pool objects."""

from __future__ import annotations

from curator.collections.collection_spec import CollectionSpec
from curator.collections.filter_predicate import GenreIn
from curator.collections.game_candidate import GameCandidate
from curator.collections.repository import CollectionsRepository


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection
        # psycopg exposes rowcount as a plain (non-async) attribute; delete_definition reads it to tell
        # "deleted" from "wasn't yours / didn't exist".
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


async def test_save_definition_returns_new_id_and_serializes_genre_filter():
    pool = FakePool(fetchone_results=[("def-1",)])
    repo = CollectionsRepository(pool)
    spec = CollectionSpec(kind="filter_list", genre_filter=("RPG", "Action"), min_score=80.0)

    definition_id = await repo.save_definition("sub-1", "My RPGs", spec)

    assert definition_id == "def-1"
    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO collection_definitions" in sql
    assert params is not None
    # The trailing element is share_slug -- generated fresh (secrets.token_urlsafe) on every call, so it
    # can only be asserted structurally, not as a literal value.
    assert params[:-1] == (
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
    assert isinstance(params[-1], str)
    assert len(params[-1]) > 0


async def test_save_definition_persists_min_percent_completed():
    pool = FakePool(fetchone_results=[("def-1",)])
    repo = CollectionsRepository(pool)
    spec = CollectionSpec(kind="filter_list", min_percent_completed=75)

    await repo.save_definition("sub-1", "Nearly Done", spec)

    _sql, params = pool.connections[0].executed[0]
    assert params is not None
    assert params[:-1] == ("sub-1", "Nearly Done", None, "filter_list", None, [], None, None, None, False, 75, None)
    assert isinstance(params[-1], str)
    assert len(params[-1]) > 0


async def test_save_definition_serializes_filter_predicate_as_json():
    pool = FakePool(fetchone_results=[("def-1",)])
    repo = CollectionsRepository(pool)
    spec = CollectionSpec(kind="filter_list", filter_predicate=GenreIn(values=("RPG", "Action")))

    await repo.save_definition("sub-1", "My RPGs", spec)

    _sql, params = pool.connections[0].executed[0]
    assert params is not None
    assert params[-2] == '{"op": "genre_in", "values": ["RPG", "Action"]}'


async def test_save_definition_leaves_filter_predicate_null_when_not_supplied():
    pool = FakePool(fetchone_results=[("def-1",)])
    repo = CollectionsRepository(pool)

    await repo.save_definition("sub-1", "My RPGs", CollectionSpec(kind="filter_list"))

    _sql, params = pool.connections[0].executed[0]
    assert params is not None
    assert params[-2] is None


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
    # Rank follows the caller's array position, not the id -- the owner's chosen order is the stored order.
    assert [call[1] for call in executed[1:]] == [("def-1", "g2", 1), ("def-1", "g1", 2)]


async def test_save_definition_drops_duplicate_game_ids():
    # collection_definition_items is keyed on (definition_id, game_id); a repeated id would otherwise
    # violate the primary key and surface as a 500 on an otherwise reasonable request.
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
    # to_spec() must carry it, or a saved "include everything I ever had" collection silently reverts to
    # active-only the next time it is re-run.
    assert definitions[0].to_spec().include_inactive is True
    assert definitions[0].to_spec().min_percent_completed == 60


async def test_list_definitions_parses_a_stored_filter_predicate():
    # psycopg auto-deserializes JSONB columns to a plain dict -- same as job_runs.result_summary's
    # existing pattern (curator.jobs.repository), so the row value here is already a dict, not a str.
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
                )
            ],
        ]
    )
    repo = CollectionsRepository(pool)

    definitions = await repo.list_definitions("sub-1")

    assert definitions[0].filter_predicate == GenreIn(values=("RPG",))
    # to_spec() must carry it, or a saved OR-predicate collection reverts to no filter at all on re-run.
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
    # Without the ::uuid[] cast Postgres compares uuid to text and the query fails outright; with it, a
    # malformed id raises InvalidTextRepresentation, which the route turns into a 400.
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
    # No owner scoping anywhere in this query: a collection's contents (and its cover art) must look the
    # same to the owner, another signed-in viewer, and an anonymous one.
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
    # The only identity in this query is the owner's, reached through collection_definitions -- never a
    # viewer's, or a shared collection would render differently depending on who opened it. The single
    # bind parameter above is the proof: there is nowhere to pass a viewer.
    assert "cd.identity_sub" in sql


async def test_list_definition_items_reports_a_game_the_owner_lost_access_to():
    # It stays in the collection -- it is the owner's curated list -- and is flagged rather than dropped.
    row = ("g1", 1, "Lapsed Title", None, None, None, None, None, None, None, False)
    pool = FakePool(fetchall_results=[[row]])
    repo = CollectionsRepository(pool)

    items = await repo.list_definition_items("def-1")

    assert items[0].owner_has_access is False


async def test_list_definition_items_falls_back_to_entitlement_artwork():
    """psn_catalog_cache only has a row once PSN catalog enrichment ran, so it cannot be the only source.

    The artwork ingestion now captures (entitlement_snapshots.title_image_url / game_icon_url /
    concept_icon_url) is the fallback leg, joined by concept id alone so it stays identity-free -- a
    collection of freshly-imported games has to render for a viewer who is not the importer.
    """
    row = ("g1", 1, "God of War", None, None, None, None, None, None, "entitlement.png", True)
    pool = FakePool(fetchall_results=[[row]])
    repo = CollectionsRepository(pool)

    items = await repo.list_definition_items("def-1")

    assert items[0].cover_image_url == "entitlement.png"
    sql, _params = pool.connections[0].executed[0]
    assert "psn_catalog_cache" in sql
    assert "entitlement_snapshots" in sql
    assert sql.index("psn_catalog_cache") < sql.index("entitlement_snapshots"), (
        "the real store cover must be preferred over entitlement artwork"
    )


async def test_list_definition_items_joins_psn_catalog_cache_by_title_id_not_product_id():
    """Regression test: psn_catalog_cache is keyed by title_id (migration 0023 renamed it from
    product_id), but this query joins it through library_entries -- a different table from
    game_concepts.product_id, which still exists and is a different identifier. A stale `pcc.product_id`
    reference here 500s at runtime (UndefinedColumn) without ever failing this FakePool-based test suite,
    since nothing here executes against a real schema -- this only locks in the known-correct SQL text."""
    row = ("g1", 1, "God of War", "God of War", "Action", "AAA", 94.0, 92.0, 4.5, "a.png", True)
    pool = FakePool(fetchall_results=[[row]])
    repo = CollectionsRepository(pool)

    await repo.list_definition_items("def-1")

    sql, _params = pool.connections[0].executed[0]
    assert "pcc.title_id" in sql
    assert "pcc.product_id" not in sql


async def test_update_definition_sets_updated_at_explicitly():
    # collection_definitions.updated_at has DEFAULT now() but no trigger, so an UPDATE that omits it
    # leaves the creation timestamp in place forever.
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
    # One connection block, so a rename can never land while its item replacement fails -- a collection
    # named for one thing holding the contents of another is worse than the edit not happening.
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
    # Scoped by identity_sub in SQL, so "someone else's" and "doesn't exist" are the same 0-row outcome.
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
