"""Tests for CollectionsRepository, using hand-written fake async psycopg_pool objects."""

from __future__ import annotations

from curator.collections.collection_spec import CollectionSpec
from curator.collections.game_candidate import GameCandidate
from curator.collections.repository import CollectionsRepository


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


async def test_list_user_consoles_maps_rows_and_computes_effective_capacity():
    pool = FakePool(fetchall_results=[[("console-1", "My PS5", "PS5", 3997.0, 200.0, ["RPG"], 0)]])
    repo = CollectionsRepository(pool)

    consoles = await repo.list_user_consoles("sub-1")

    assert len(consoles) == 1
    assert consoles[0].console_id == "console-1"
    assert consoles[0].effective_capacity_gb == 3797.0
    assert consoles[0].routing_genres == ("RPG",)


async def test_list_candidates_no_platform_filter():
    pool = FakePool(
        fetchall_results=[[("game-1", "God of War", "Action", "AAA", "God of War", 90.0, 85.0, None, False, None)]]
    )
    repo = CollectionsRepository(pool)

    candidates = await repo.list_candidates("sub-1")

    assert len(candidates) == 1
    assert candidates[0].game_id == "game-1"
    assert candidates[0].critical_score == 90.0
    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "native_ps5 = true" not in sql
    assert "ps4_eligible = true" not in sql
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


async def test_save_definition_returns_new_id_and_serializes_genre_filter():
    pool = FakePool(fetchone_results=[("def-1",)])
    repo = CollectionsRepository(pool)
    spec = CollectionSpec(kind="filter_list", genre_filter=("RPG", "Action"), min_score=80.0)

    definition_id = await repo.save_definition("sub-1", "My RPGs", spec)

    assert definition_id == "def-1"
    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO collection_definitions" in sql
    assert params == ("sub-1", "My RPGs", "filter_list", None, ["RPG", "Action"], 80.0, None, None)


async def test_list_definitions_maps_rows():
    pool = FakePool(
        fetchall_results=[
            [("def-1", "sub-1", "My RPGs", "filter_list", None, ["RPG"], 80.0, None, None)],
        ]
    )
    repo = CollectionsRepository(pool)

    definitions = await repo.list_definitions("sub-1")

    assert len(definitions) == 1
    assert definitions[0].definition_id == "def-1"
    assert definitions[0].genre_filter == ("RPG",)
    assert definitions[0].min_score == 80.0


async def test_get_definition_scopes_to_identity_sub():
    pool = FakePool(fetchone_results=[("def-1", "sub-1", "My RPGs", "filter_list", None, ["RPG"], 80.0, None, None)])
    repo = CollectionsRepository(pool)

    definition = await repo.get_definition("sub-1", "def-1")

    assert definition is not None
    assert definition.name == "My RPGs"
    _sql, params = pool.connections[0].executed[0]
    assert params == ("sub-1", "def-1")


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
