"""Tests for CollectionsRepository, using hand-written fake async psycopg_pool objects."""

from __future__ import annotations

import json

import pytest

from curator.catalog.cover_art import SQUARE_COVER_ART_SQL
from curator.collections.collection_spec import CAPACITY_FILL_KIND, FILTER_LIST_KIND, CollectionSpec
from curator.collections.filter_predicate import (
    GENRE_IN_OP,
    PREDICATE_OP_KEY,
    PREDICATE_VALUES_KEY,
    GenreIn,
    predicate_to_dict,
)
from curator.collections.game_candidate import GameCandidate
from curator.collections.repository import (
    BROWSABLE_CANDIDATE_SQL,
    IS_FREE_TO_PLAY_SQL,
    STORAGE_DEVICES_INSTALLED_GAME_IDS_SQL,
    VISIBILITY_PRIVATE,
    VISIBILITY_UNLISTED,
    CollectionsRepository,
)
from curator.psn.title_platform import CONSOLE_PLATFORM_IDS, PS4, PS5, PSP, PSVITA
from test_values import (
    new_console_id,
    new_cover_image_url,
    new_definition_id,
    new_game_id,
    new_game_title,
    new_genre_name,
    new_identity_sub,
    new_np_communication_id,
    new_percent_completed,
    new_positive_count,
    new_psn_rating,
    new_review_score,
    new_run_id,
    new_share_slug,
    new_size_gb,
    new_small_count,
    new_storage_device_id,
    new_utc_instant,
)

CONSOLE_MODEL = new_game_title()


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


def _candidate() -> GameCandidate:
    return GameCandidate(
        game_id=new_game_id(),
        title=new_game_title(),
        genre=new_genre_name(),
        aaa_tier=new_genre_name(),
        franchise=new_game_title(),
        composite_score=new_review_score(),
        rank_score=new_small_count(),
        size_gb=new_size_gb(),
    )


async def test_groups_installed_game_ids_under_each_named_device_and_keeps_a_device_with_none():
    first_device_id, second_device_id, bare_device_id = (
        new_storage_device_id(),
        new_storage_device_id(),
        new_storage_device_id(),
    )
    first_game_id, second_game_id, third_game_id = new_game_id(), new_game_id(), new_game_id()
    pool = FakePool(
        fetchall_results=[
            [
                (first_device_id, first_game_id),
                (first_device_id, second_game_id),
                (second_device_id, third_game_id),
            ]
        ]
    )

    installed = await CollectionsRepository(pool).list_installed_game_ids_by_storage_device(
        [first_device_id, second_device_id, bare_device_id]
    )

    assert installed == {
        first_device_id: {first_game_id, second_game_id},
        second_device_id: {third_game_id},
        bare_device_id: set(),
    }
    assert pool.connections[0].executed == [
        (STORAGE_DEVICES_INSTALLED_GAME_IDS_SQL, ([first_device_id, second_device_id, bare_device_id],))
    ]


async def test_list_installed_game_ids_by_storage_device_asks_nothing_when_no_device_is_named():
    pool = FakePool()

    installed = await CollectionsRepository(pool).list_installed_game_ids_by_storage_device([])

    assert installed == {}
    assert pool.connections == []


async def test_list_user_consoles_maps_rows_and_computes_effective_capacity():
    console_id = new_console_id()
    raw_capacity_gb, update_buffer_gb = new_size_gb(), new_size_gb()
    routing_genre = new_genre_name()
    pool = FakePool(
        fetchall_results=[
            [
                (
                    console_id,
                    new_game_title(),
                    PS5,
                    raw_capacity_gb,
                    update_buffer_gb,
                    [routing_genre],
                    0,
                    CONSOLE_MODEL,
                )
            ]
        ]
    )
    repo = CollectionsRepository(pool)

    consoles = await repo.list_user_consoles(new_identity_sub())

    assert len(consoles) == 1
    assert consoles[0].console_id == console_id
    assert consoles[0].effective_capacity_gb == raw_capacity_gb - update_buffer_gb
    assert consoles[0].routing_genres == (routing_genre,)
    assert consoles[0].model == CONSOLE_MODEL


async def test_list_candidates_no_platform_filter():
    identity_sub = new_identity_sub()
    game_id = new_game_id()
    critical_score = new_review_score()
    np_communication_id = new_np_communication_id()
    percent_completed = new_percent_completed()
    game_title = new_game_title()
    pool = FakePool(
        fetchall_results=[
            [
                (
                    game_id,
                    game_title,
                    new_genre_name(),
                    new_genre_name(),
                    game_title,
                    critical_score,
                    new_review_score(),
                    None,
                    False,
                    None,
                    np_communication_id,
                    percent_completed,
                    None,
                )
            ]
        ]
    )
    repo = CollectionsRepository(pool)

    candidates = await repo.list_candidates(identity_sub)

    assert len(candidates) == 1
    assert candidates[0].game_id == game_id
    assert candidates[0].critical_score == critical_score
    assert candidates[0].np_communication_id == np_communication_id
    assert candidates[0].percent_completed == percent_completed
    assert candidates[0].download_size_bytes is None
    conn = pool.connections[0]
    sql, params = conn.executed[0]
    assert "lep.platform = %s" not in sql
    assert params == (identity_sub,)


async def test_list_candidates_applies_the_completion_floor_in_sql():
    identity_sub = new_identity_sub()
    completion_floor = new_percent_completed()
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(identity_sub, min_percent_completed=completion_floor)

    sql, params = pool.connections[0].executed[0]
    assert "le.trophy_percent_completed >= %s" in sql
    assert params == (identity_sub, completion_floor)


async def test_completion_floor_is_skipped_for_a_user_with_no_stored_progress():
    """A floor must not empty a collection for a user who has never had a trophy refresh.

    Their rows are all NULL, so a plain ``>=`` would exclude everything -- for a reason that has nothing
    to do with the collection. The NOT EXISTS escape hatch is the SQL equivalent of the
    ``completion_available`` guard the in-memory filter uses.
    """
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(new_identity_sub(), min_percent_completed=new_percent_completed())

    sql, _params = pool.connections[0].executed[0]
    assert "NOT EXISTS" in sql
    assert "probe.trophy_percent_completed IS NOT NULL" in sql


async def test_list_candidates_omits_the_completion_clause_when_no_floor_is_set():
    identity_sub = new_identity_sub()
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(identity_sub)

    sql, params = pool.connections[0].executed[0]
    assert "le.trophy_percent_completed >= %s" not in sql
    assert "NOT EXISTS (\n                        SELECT 1 FROM library_entries probe" not in sql
    assert params == (identity_sub,)


@pytest.mark.parametrize("platform", CONSOLE_PLATFORM_IDS)
async def test_list_candidates_filters_every_platform_through_library_entry_platforms(platform):
    """The boolean pair could only express PS5 and PS4, so a PS3/Vita/PSP console fell through to an
    unfiltered candidate list -- every game in the library, including PS5-only ones."""
    identity_sub = new_identity_sub()
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(identity_sub, platform=platform)

    sql, params = pool.connections[0].executed[0]
    assert "FROM library_entry_platforms lep" in sql
    assert "le.native_ps5" not in sql, "the boolean pair must not decide platform membership any more"
    assert "le.ps4_eligible" not in sql
    assert params == (platform, platform, identity_sub, platform)


async def test_list_candidates_sizes_an_unfiltered_run_from_the_platforms_of_record():
    """``native_ps5`` is a legacy boolean the platforms table superseded; a run with no platform filter
    still has to size a PS5-owned game as PS5, read from ``library_entry_platforms``."""
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(new_identity_sub())

    sql, _params = pool.connections[0].executed[0]
    assert "le.native_ps5" not in sql
    assert "FROM library_entry_platforms owned" in sql
    assert "owned.platform = 'PS5'" in sql


async def test_list_candidates_reads_the_measured_size_for_the_platform_being_filled():
    """A game owned on both PS4 and PS5 has two game_measured_sizes rows. The pair-derived CASE always
    picked the PS5 one, so a PS4 capacity fill packed against PS5 sizes."""
    identity_sub = new_identity_sub()
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(identity_sub, platform=PS4)

    sql, params = pool.connections[0].executed[0]
    assert "CASE WHEN le.native_ps5" not in sql
    assert params == (PS4, PS4, identity_sub, PS4), (
        "the measured-size and download-size subqueries are in the SELECT list, so their parameters bind "
        "before the WHERE clause's"
    )


async def test_list_candidates_reads_the_download_size_for_the_platform_being_filled():
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(new_identity_sub(), platform=PSVITA)

    sql, _params = pool.connections[0].executed[0]
    assert "FROM game_download_sizes gds" in sql
    assert "gds.platform = (%s)" in sql


async def test_list_candidates_reads_free_to_play_from_the_walked_store_price():
    """game_enrichment.is_free_to_play was NULL on every row and written by nothing, so the free-to-play
    adjustment never fired; the storefront's own price node is the source."""
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(new_identity_sub())

    sql, _params = pool.connections[0].executed[0]
    assert IS_FREE_TO_PLAY_SQL in sql
    assert "ge.is_free_to_play" not in sql


async def test_list_candidates_leaves_proven_non_games_out_by_default():
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(new_identity_sub())

    sql, _params = pool.connections[0].executed[0]
    assert BROWSABLE_CANDIDATE_SQL in sql


async def test_list_candidates_keeps_non_games_when_a_capacity_fill_asks():
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(new_identity_sub(), include_non_games=True)

    sql, _params = pool.connections[0].executed[0]
    assert "content_kind" not in sql


async def test_user_has_trophy_data_probes_for_any_stored_percentage():
    identity_sub = new_identity_sub()
    pool = FakePool(fetchone_results=[(True,)])
    repo = CollectionsRepository(pool)

    assert await repo.user_has_trophy_data(identity_sub) is True
    sql, params = pool.connections[0].executed[0]
    assert "trophy_percent_completed IS NOT NULL" in sql
    assert params == (identity_sub,)


async def test_counting_candidates_missing_trophy_data_uses_the_candidate_predicates_without_the_floor():
    identity_sub = new_identity_sub()
    excluded_console_id = new_console_id()
    pool = FakePool(fetchone_results=[(new_positive_count(),)])
    repo = CollectionsRepository(pool)

    await repo.count_candidates_missing_trophy_data(
        identity_sub, platform=PS5, exclude_installed_on=(excluded_console_id,)
    )

    sql, params = pool.connections[0].executed[0]
    assert "le.trophy_percent_completed IS NULL" in sql
    assert "le.trophy_percent_completed >= %s" not in sql
    assert "FROM library_entry_platforms lep" in sql
    assert "console_installs" in sql
    assert "library_exclusions" in sql
    assert params == (identity_sub, PS5, identity_sub, [excluded_console_id])


async def test_platform_media_ceilings_map_only_platforms_that_carry_one():
    psp_ceiling_gb, vita_ceiling_gb = new_size_gb(), new_size_gb()
    pool = FakePool(fetchall_results=[[(PSP, psp_ceiling_gb), (PSVITA, vita_ceiling_gb)]])
    repo = CollectionsRepository(pool)

    ceilings = await repo.list_platform_media_ceilings()

    assert ceilings == {PSP: psp_ceiling_gb, PSVITA: vita_ceiling_gb}
    sql, _params = pool.connections[0].executed[0]
    assert "media_ceiling_gb IS NOT NULL" in sql


async def test_list_candidates_excludes_inactive_entitlements_by_default():
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(new_identity_sub())

    sql, _params = pool.connections[0].executed[0]
    assert "le.is_active = true" in sql


async def test_list_candidates_include_inactive_drops_the_active_clause():
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(new_identity_sub(), include_inactive=True)

    sql, _params = pool.connections[0].executed[0]
    assert "is_active" not in sql


async def test_list_candidates_orders_deterministically():
    """Bin-packing (curator.collections.capacity_fill_strategy) is size-order-sensitive, and Python's
    sorted() is stable -- so ties at equal composite score need a deterministic starting order, or the
    same collection could pack differently between two runs. See sort_order's own module docstring."""
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(new_identity_sub())

    sql, _params = pool.connections[0].executed[0]
    assert "ORDER BY g.canonical_title, g.game_id" in sql


async def test_list_candidates_omits_the_installed_elsewhere_clause_when_not_asked():
    identity_sub = new_identity_sub()
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(identity_sub)

    sql, params = pool.connections[0].executed[0]
    assert "console_installs" not in sql
    assert params == (identity_sub,)


async def test_list_candidates_excludes_games_installed_on_the_given_consoles():
    identity_sub = new_identity_sub()
    first_console_id, second_console_id = new_console_id(), new_console_id()
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_candidates(identity_sub, exclude_installed_on=(first_console_id, second_console_id))

    sql, params = pool.connections[0].executed[0]
    assert "console_installs" in sql
    assert "ci.installed = true" in sql
    assert "uc.identity_sub = %s" in sql
    assert params == (identity_sub, identity_sub, [first_console_id, second_console_id])


async def test_save_definition_returns_new_id_and_serializes_genre_filter():
    identity_sub = new_identity_sub()
    stored_definition_id = new_definition_id()
    name = new_game_title()
    first_genre, second_genre = new_genre_name(), new_genre_name()
    min_score = new_review_score()
    pool = FakePool(fetchone_results=[(stored_definition_id,)])
    repo = CollectionsRepository(pool)
    spec = CollectionSpec(kind=FILTER_LIST_KIND, genre_filter=(first_genre, second_genre), min_score=min_score)

    definition_id = await repo.save_definition(identity_sub, name, spec)

    assert definition_id == stored_definition_id
    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO collection_definitions" in sql
    assert params is not None
    assert params == (
        identity_sub,
        name,
        None,
        FILTER_LIST_KIND,
        None,
        [first_genre, second_genre],
        min_score,
        None,
        None,
        False,
        None,
        None,
        params[-3],
        [],
        None,
    )
    assert isinstance(params[-3], str)
    assert len(params[-3]) > 0
    assert params[-2] == []
    assert params[-1] is None


async def test_save_definition_persists_min_percent_completed():
    identity_sub = new_identity_sub()
    name = new_game_title()
    completion_floor = new_percent_completed()
    pool = FakePool(fetchone_results=[(new_definition_id(),)])
    repo = CollectionsRepository(pool)
    spec = CollectionSpec(kind=FILTER_LIST_KIND, min_percent_completed=completion_floor)

    await repo.save_definition(identity_sub, name, spec)

    _sql, params = pool.connections[0].executed[0]
    assert params is not None
    assert params == (
        identity_sub,
        name,
        None,
        FILTER_LIST_KIND,
        None,
        [],
        None,
        None,
        None,
        False,
        completion_floor,
        None,
        params[-3],
        [],
        None,
    )
    assert params[-1] is None, "a new collection targets no console until one is assigned"
    assert isinstance(params[-3], str)
    assert len(params[-3]) > 0


async def test_save_definition_persists_exclude_installed_on():
    first_console_id, second_console_id = new_console_id(), new_console_id()
    pool = FakePool(fetchone_results=[(new_definition_id(),)])
    repo = CollectionsRepository(pool)
    spec = CollectionSpec(kind=FILTER_LIST_KIND, exclude_installed_on=(first_console_id, second_console_id))

    await repo.save_definition(new_identity_sub(), new_game_title(), spec)

    _sql, params = pool.connections[0].executed[0]
    assert params is not None
    assert params[-2] == [first_console_id, second_console_id]


async def test_save_definition_serializes_filter_predicate_as_json():
    first_genre, second_genre = new_genre_name(), new_genre_name()
    pool = FakePool(fetchone_results=[(new_definition_id(),)])
    repo = CollectionsRepository(pool)
    spec = CollectionSpec(kind=FILTER_LIST_KIND, filter_predicate=GenreIn(values=(first_genre, second_genre)))

    await repo.save_definition(new_identity_sub(), new_game_title(), spec)

    _sql, params = pool.connections[0].executed[0]
    assert params is not None
    assert params[-4] == json.dumps({PREDICATE_OP_KEY: GENRE_IN_OP, PREDICATE_VALUES_KEY: [first_genre, second_genre]})


async def test_save_definition_leaves_filter_predicate_null_when_not_supplied():
    pool = FakePool(fetchone_results=[(new_definition_id(),)])
    repo = CollectionsRepository(pool)

    await repo.save_definition(new_identity_sub(), new_game_title(), CollectionSpec(kind=FILTER_LIST_KIND))

    _sql, params = pool.connections[0].executed[0]
    assert params is not None
    assert params[-4] is None


async def test_save_definition_stores_membership_ranked_by_position():
    definition_id = new_definition_id()
    description = new_game_title()
    first_game_id, second_game_id = new_game_id(), new_game_id()
    pool = FakePool(fetchone_results=[(definition_id,)])
    repo = CollectionsRepository(pool)

    await repo.save_definition(
        new_identity_sub(),
        new_game_title(),
        CollectionSpec(kind=FILTER_LIST_KIND),
        description=description,
        game_ids=(second_game_id, first_game_id),
    )

    executed = pool.connections[0].executed
    assert executed[0][1] is not None
    assert executed[0][1][2] == description
    assert "INSERT INTO collection_definition_items" in executed[1][0]
    assert [call[1] for call in executed[1:]] == [(definition_id, second_game_id, 1), (definition_id, first_game_id, 2)]


async def test_save_definition_drops_duplicate_game_ids():
    definition_id = new_definition_id()
    first_game_id, second_game_id = new_game_id(), new_game_id()
    pool = FakePool(fetchone_results=[(definition_id,)])
    repo = CollectionsRepository(pool)

    await repo.save_definition(
        new_identity_sub(),
        new_game_title(),
        CollectionSpec(kind=FILTER_LIST_KIND),
        game_ids=(first_game_id, second_game_id, first_game_id),
    )

    item_inserts = [call for call in pool.connections[0].executed if "collection_definition_items" in call[0]]
    assert [call[1] for call in item_inserts] == [(definition_id, first_game_id, 1), (definition_id, second_game_id, 2)]


async def test_list_definitions_maps_rows():
    identity_sub = new_identity_sub()
    definition_id = new_definition_id()
    genre = new_genre_name()
    min_score = new_review_score()
    description = new_game_title()
    completion_floor = new_percent_completed()
    share_slug = new_share_slug()
    item_count = new_positive_count()
    console_id = new_console_id()
    pool = FakePool(
        fetchall_results=[
            [
                (
                    definition_id,
                    identity_sub,
                    new_game_title(),
                    FILTER_LIST_KIND,
                    None,
                    [genre],
                    min_score,
                    None,
                    None,
                    description,
                    True,
                    completion_floor,
                    None,
                    VISIBILITY_UNLISTED,
                    share_slug,
                    item_count,
                    [console_id],
                    None,
                )
            ],
        ]
    )
    repo = CollectionsRepository(pool)

    definitions = await repo.list_definitions(identity_sub)

    assert len(definitions) == 1
    assert definitions[0].definition_id == definition_id
    assert definitions[0].genre_filter == (genre,)
    assert definitions[0].visibility == VISIBILITY_UNLISTED
    assert definitions[0].share_slug == share_slug
    assert definitions[0].item_count == item_count
    assert definitions[0].min_score == min_score
    assert definitions[0].description == description
    assert definitions[0].include_inactive is True
    assert definitions[0].min_percent_completed == completion_floor
    assert definitions[0].filter_predicate is None
    assert definitions[0].exclude_installed_on == (console_id,)
    assert definitions[0].to_spec().include_inactive is True
    assert definitions[0].to_spec().min_percent_completed == completion_floor
    assert definitions[0].to_spec().exclude_installed_on == (console_id,)


async def test_list_definitions_parses_a_stored_filter_predicate():
    identity_sub = new_identity_sub()
    predicate = GenreIn(values=(new_genre_name(),))
    pool = FakePool(
        fetchall_results=[
            [
                (
                    new_definition_id(),
                    identity_sub,
                    new_game_title(),
                    FILTER_LIST_KIND,
                    None,
                    [],
                    None,
                    None,
                    None,
                    None,
                    False,
                    None,
                    predicate_to_dict(predicate),
                    VISIBILITY_PRIVATE,
                    None,
                    0,
                    [],
                    None,
                )
            ],
        ]
    )
    repo = CollectionsRepository(pool)

    definitions = await repo.list_definitions(identity_sub)

    assert definitions[0].filter_predicate == predicate
    assert definitions[0].to_spec().filter_predicate == predicate


async def test_get_definition_scopes_to_identity_sub():
    identity_sub = new_identity_sub()
    definition_id = new_definition_id()
    name = new_game_title()
    pool = FakePool(
        fetchone_results=[
            (
                definition_id,
                identity_sub,
                name,
                FILTER_LIST_KIND,
                None,
                [new_genre_name()],
                new_review_score(),
                None,
                None,
                None,
                False,
                None,
                None,
                VISIBILITY_PRIVATE,
                new_share_slug(),
                0,
                [],
                None,
            )
        ]
    )
    repo = CollectionsRepository(pool)

    definition = await repo.get_definition(identity_sub, definition_id)

    assert definition is not None
    assert definition.name == name
    assert definition.min_percent_completed is None
    _sql, params = pool.connections[0].executed[0]
    assert params == (identity_sub, definition_id)


async def test_count_definitions_for_a_non_owner_filters_on_public_in_sql():
    profile_owner_sub = new_identity_sub()
    public_definition_count = new_positive_count()
    pool = FakePool(fetchone_results=[(public_definition_count,)])
    repo = CollectionsRepository(pool)

    count = await repo.count_definitions(profile_owner_sub, public_only=True)

    sql, params = pool.connections[0].executed[0]
    assert count == public_definition_count
    assert "visibility = 'public'" in sql
    assert "!= 'private'" not in sql
    assert params == (profile_owner_sub,)


async def test_count_definitions_for_the_owner_carries_no_visibility_clause_at_all():
    profile_owner_sub = new_identity_sub()
    owned_definition_count = new_positive_count()
    pool = FakePool(fetchone_results=[(owned_definition_count,)])
    repo = CollectionsRepository(pool)

    count = await repo.count_definitions(profile_owner_sub)

    sql, params = pool.connections[0].executed[0]
    assert count == owned_definition_count
    assert "visibility" not in sql
    assert params == (profile_owner_sub,)


async def test_get_definition_by_share_slug_filters_on_not_private_in_sql():
    shared_slug = new_share_slug()
    pool = FakePool()
    repo = CollectionsRepository(pool)

    await repo.get_definition_by_share_slug(shared_slug)

    sql, params = pool.connections[0].executed[0]
    assert "cd.share_slug = %s" in sql
    assert "cd.visibility != 'private'" in sql
    assert params == (shared_slug,)


async def test_the_share_slug_predicate_admits_unlisted_while_the_public_count_does_not():
    profile_owner_sub = new_identity_sub()
    shared_slug = new_share_slug()
    count_pool = FakePool(fetchone_results=[(new_positive_count(),)])
    slug_pool = FakePool()

    await CollectionsRepository(count_pool).count_definitions(profile_owner_sub, public_only=True)
    await CollectionsRepository(slug_pool).get_definition_by_share_slug(shared_slug)

    count_sql, _count_params = count_pool.connections[0].executed[0]
    slug_sql, _slug_params = slug_pool.connections[0].executed[0]
    assert "visibility = 'public'" in count_sql
    assert "visibility != 'private'" in slug_sql
    assert "visibility != 'private'" not in count_sql
    assert "visibility = 'public'" not in slug_sql


async def test_existing_game_ids_casts_to_uuid_array():
    known_game_id, unknown_game_id = new_game_id(), new_game_id()
    pool = FakePool(fetchall_results=[[(known_game_id,)]])
    repo = CollectionsRepository(pool)

    known = await repo.existing_game_ids((known_game_id, unknown_game_id))

    assert known == {known_game_id}
    sql, params = pool.connections[0].executed[0]
    assert "ANY(%s::uuid[])" in sql
    assert params == ([known_game_id, unknown_game_id],)


async def test_existing_game_ids_short_circuits_on_empty_input():
    pool = FakePool()
    repo = CollectionsRepository(pool)

    assert await repo.existing_game_ids(()) == set()
    assert pool.connections == []


async def test_list_definition_items_takes_no_identity_sub_and_orders_by_rank():
    definition_id = new_definition_id()
    title = new_game_title()
    cover_image_url = new_cover_image_url()
    row = (
        new_game_id(),
        1,
        title,
        title,
        new_genre_name(),
        new_genre_name(),
        new_review_score(),
        new_review_score(),
        new_psn_rating(),
        cover_image_url,
        True,
        None,
    )
    pool = FakePool(fetchall_results=[[row]])
    repo = CollectionsRepository(pool)

    items = await repo.list_definition_items(definition_id)

    assert len(items) == 1
    assert items[0].rank == 1
    assert items[0].title == title
    assert items[0].cover_image_url == cover_image_url
    assert items[0].owner_has_access is True
    sql, params = pool.connections[0].executed[0]
    assert params == (definition_id,)
    assert "ORDER BY cdi.rank" in sql
    assert "cd.identity_sub" in sql


async def test_list_definition_items_reports_a_game_the_owner_lost_access_to():
    row = (new_game_id(), 1, new_game_title(), None, None, None, None, None, None, None, False, None)
    pool = FakePool(fetchall_results=[[row]])
    repo = CollectionsRepository(pool)

    items = await repo.list_definition_items(new_definition_id())

    assert items[0].owner_has_access is False


async def test_list_definition_items_reads_entitlement_artwork():
    """Collection items carry the square entitlement icon, identity-free so a collection of
    freshly-imported games renders for a viewer who is not the importer."""
    cover_image_url = new_cover_image_url()
    row = (new_game_id(), 1, new_game_title(), None, None, None, None, None, None, cover_image_url, True, None)
    pool = FakePool(fetchall_results=[[row]])
    repo = CollectionsRepository(pool)

    items = await repo.list_definition_items(new_definition_id())

    assert items[0].cover_image_url == cover_image_url
    sql, _params = pool.connections[0].executed[0]
    assert "entitlement_snapshots" in sql


async def test_list_definition_items_never_serves_storefront_hero_art():
    """``psn_catalog_cache`` holds 16:9 key art, which would render this list at a different shape from
    every other surface showing the same games."""
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_definition_items(new_definition_id())

    sql, _params = pool.connections[0].executed[0]
    assert "psn_catalog_cache" not in sql
    assert "pcc." not in sql


async def test_list_definition_items_uses_the_shared_cover_art_expression():
    """Every cover-returning query interpolates the same constant, so none can drift into a second
    artwork policy. Nothing in this FakePool suite executes against a real schema, so a column that
    exists in no table would otherwise only surface as an UndefinedColumn 500 at runtime."""
    pool = FakePool(fetchall_results=[[]])
    repo = CollectionsRepository(pool)

    await repo.list_definition_items(new_definition_id())

    sql, _params = pool.connections[0].executed[0]
    assert SQUARE_COVER_ART_SQL in sql


async def test_update_definition_sets_updated_at_explicitly():
    definition_id = new_definition_id()
    new_name = new_game_title()
    pool = FakePool()
    repo = CollectionsRepository(pool)

    await repo.update_definition(definition_id, name=new_name, description=None)

    sql, params = pool.connections[0].executed[0]
    assert "updated_at = now()" in sql
    assert params == (new_name, None, definition_id)


async def test_update_definition_leaves_membership_alone_when_game_ids_is_none():
    pool = FakePool()
    repo = CollectionsRepository(pool)

    await repo.update_definition(new_definition_id(), name=new_game_title(), description=None)

    assert len(pool.connections[0].executed) == 1


async def test_update_definition_replaces_membership_in_the_same_transaction():
    definition_id = new_definition_id()
    first_game_id, second_game_id = new_game_id(), new_game_id()
    pool = FakePool()
    repo = CollectionsRepository(pool)

    await repo.update_definition(
        definition_id, name=new_game_title(), description=None, game_ids=(first_game_id, second_game_id)
    )

    assert len(pool.connections) == 1
    executed = pool.connections[0].executed
    assert "UPDATE collection_definitions" in executed[0][0]
    assert "DELETE FROM collection_definition_items" in executed[1][0]
    assert [call[1] for call in executed[2:]] == [(definition_id, first_game_id, 1), (definition_id, second_game_id, 2)]


async def test_delete_definition_reports_whether_a_row_went():
    identity_sub = new_identity_sub()
    definition_id = new_definition_id()
    pool = FakePool(rowcount=1)
    repo = CollectionsRepository(pool)

    assert await repo.delete_definition(identity_sub, definition_id) is True
    _sql, params = pool.connections[0].executed[0]
    assert params == (identity_sub, definition_id)


async def test_delete_definition_returns_false_when_not_the_callers():
    pool = FakePool(rowcount=0)
    repo = CollectionsRepository(pool)

    assert await repo.delete_definition(new_identity_sub(), new_definition_id()) is False


async def test_get_definition_returns_none_when_not_found():
    pool = FakePool(fetchone_results=[None])
    repo = CollectionsRepository(pool)

    definition = await repo.get_definition(new_identity_sub(), new_definition_id())

    assert definition is None


async def test_save_run_writes_run_and_items():
    run_id_stored = new_run_id()
    pool = FakePool(fetchone_results=[(run_id_stored,)])
    repo = CollectionsRepository(pool)
    included_candidate = _candidate()
    excluded_candidate = _candidate()

    run_id = await repo.save_run(
        new_identity_sub(), None, {"kind": CAPACITY_FILL_KIND}, [included_candidate], [excluded_candidate]
    )

    assert run_id == run_id_stored
    conn = pool.connections[0]
    assert "INSERT INTO collection_runs" in conn.executed[0][0]
    assert "INSERT INTO collection_items" in conn.executed[1][0]
    included_params = conn.executed[1][1]
    assert included_params is not None
    assert included_params[:3] == (run_id_stored, included_candidate.game_id, 1)
    assert "INSERT INTO collection_items" in conn.executed[2][0]
    excluded_params = conn.executed[2][1]
    assert excluded_params is not None
    assert excluded_params[:2] == (run_id_stored, excluded_candidate.game_id)


async def test_list_measured_sizes_maps_rows():
    game_id = new_game_id()
    size_gb = new_size_gb()
    recorded_by = new_identity_sub()
    recorded_at = new_utc_instant()
    pool = FakePool(fetchall_results=[[(game_id, PS5, size_gb, recorded_by, recorded_at)]])
    repo = CollectionsRepository(pool)

    sizes = await repo.list_measured_sizes(game_id)

    assert len(sizes) == 1
    assert sizes[0].game_id == game_id
    assert sizes[0].platform == PS5
    assert sizes[0].size_gb == size_gb
    assert sizes[0].recorded_by == recorded_by
    assert sizes[0].recorded_at == recorded_at


async def test_list_measured_sizes_reports_no_contributor_after_recorded_by_is_set_null():
    game_id = new_game_id()
    pool = FakePool(fetchall_results=[[(game_id, PS5, new_size_gb(), None, new_utc_instant())]])
    repo = CollectionsRepository(pool)

    sizes = await repo.list_measured_sizes(game_id)

    assert sizes[0].recorded_by is None


async def test_upsert_measured_size_upserts_on_game_and_platform():
    game_id = new_game_id()
    size_gb = new_size_gb()
    recorded_by = new_identity_sub()
    recorded_at = new_utc_instant()
    pool = FakePool(fetchone_results=[(recorded_at,)])
    repo = CollectionsRepository(pool)

    measured_size = await repo.upsert_measured_size(game_id, PS5, size_gb, recorded_by)

    assert measured_size.game_id == game_id
    assert measured_size.platform == PS5
    assert measured_size.size_gb == size_gb
    assert measured_size.recorded_by == recorded_by
    assert measured_size.recorded_at == recorded_at
    conn = pool.connections[0]
    assert "ON CONFLICT (game_id, platform) DO UPDATE" in conn.executed[0][0]
    assert conn.executed[0][1] == (game_id, PS5, size_gb, recorded_by)


async def test_is_following_collection_is_true_when_a_follow_row_exists():
    follower_sub = new_identity_sub()
    definition_id = new_definition_id()
    pool = FakePool(fetchone_results=[(1,)])
    repo = CollectionsRepository(pool)

    assert await repo.is_following_collection(follower_sub, definition_id) is True
    sql, params = pool.connections[0].executed[0]
    assert params == (follower_sub, definition_id)
    assert "FROM collection_follows" in sql


async def test_is_following_collection_is_false_when_no_row_comes_back():
    pool = FakePool(fetchone_results=[None])
    repo = CollectionsRepository(pool)

    assert await repo.is_following_collection(new_identity_sub(), new_definition_id()) is False


async def test_collection_follower_count_returns_the_scalar_scoped_to_one_collection():
    definition_id = new_definition_id()
    follower_count = new_positive_count()
    pool = FakePool(fetchone_results=[(follower_count,)])
    repo = CollectionsRepository(pool)

    assert await repo.collection_follower_count(definition_id) == follower_count
    sql, params = pool.connections[0].executed[0]
    assert params == (definition_id,)
    assert "count(*)" in sql
    assert "WHERE definition_id = %s" in sql


async def test_collection_follower_count_is_zero_when_nobody_follows():
    pool = FakePool(fetchone_results=[(0,)])
    repo = CollectionsRepository(pool)

    assert await repo.collection_follower_count(new_definition_id()) == 0
