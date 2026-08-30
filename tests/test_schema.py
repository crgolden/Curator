"""Integration tests for every ``db/migrations/*.sql`` file, applied in order to a **real** PostgreSQL
instance.

These are the only tests in this suite that touch a live database. They are gated on
``CURATOR_TEST_DATABASE_URL``; ``Curator/TESTING.md`` owns how to set it, locally and in CI. Point it at a
database you are willing to have migrated — never production.

The schema is built by ``db/run_migrations.py``, the same runner the deploy job uses, which applies only
what its ``schema_migrations`` table does not already record.

**This module commits**: the target database is left migrated. Individual tests leave no trace — each runs
in a ``SAVEPOINT`` rolled back in teardown.

The module shares one connection, so it carries an ``xdist_group`` mark: under ``-n``/``--dist loadgroup``
every test lands on one worker rather than contending on the same migration locks.

Example (PowerShell):

    python -m pytest tests/test_schema.py -q -n0
"""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg import errors as psycopg_errors

from curator.collections.repository import _ITEM_BASE_FROM, _ITEM_SELECT_COLUMNS, CollectionsRepository
from curator.psn.title_platform import CONSOLE_PLATFORM_IDS

DATABASE_URL = os.environ.get("CURATOR_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.skipif(
        not DATABASE_URL,
        reason=(
            "CURATOR_TEST_DATABASE_URL is not set; schema integration tests only run against an explicitly "
            "configured disposable PostgreSQL database. See this module's docstring."
        ),
    ),
    pytest.mark.xdist_group("schema"),
]

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

RUN_MIGRATIONS_PATH = Path(__file__).resolve().parent.parent / "db" / "run_migrations.py"

EXPECTED_TABLES = {
    "app_users",
    "psn_links",
    "psn_test_accounts",
    "entitlement_pulls",
    "entitlement_snapshots",
    "games",
    "game_concepts",
    "game_name_overrides",
    "genres",
    "game_enrichment",
    "rawg_cache",
    "opencritic_cache",
    "psn_catalog_cache",
    "data_quality_flags",
    "data_quality_flag_games",
    "exclusion_rules",
    "global_exclusions",
    "franchise_rules",
    "edition_ranks",
    "publisher_tiers",
    "size_estimates",
    "library_entries",
    "library_exclusions",
    "user_consoles",
    "game_measured_sizes",
    "collection_definitions",
    "collection_definition_items",
    "collection_runs",
    "collection_items",
    "console_installs",
    "job_runs",
    "user_enrichment_keys",
    "opencritic_pagination_cursor",
    "account_action_log",
    "user_profiles",
    "follows",
}


def _run_migrations(database_url: str) -> None:
    """Apply pending migrations via ``db/run_migrations.py``, loaded by path because ``db/`` is a payload
    directory rather than an importable package."""
    spec = importlib.util.spec_from_file_location("curator_db_run_migrations", RUN_MIGRATIONS_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.run_migrations(database_url)


@pytest.fixture(scope="session")
def migrated_connection():
    """Migrate the target database, then open one connection shared by every test in the session.

    Leaves the database migrated rather than pristine; per-test isolation is ``db_connection``'s savepoint.

    :raises RuntimeError: If the target database name does not end in ``_test``.
    """
    database = urlsplit(DATABASE_URL).path.lstrip("/")
    if not database.endswith("_test"):
        raise RuntimeError(
            f"CURATOR_TEST_DATABASE_URL names {database!r}, which is not a *_test database. This module "
            "migrates and commits, so pointing it at the exploratory 'curator' database would alter the "
            "data kept there for manual work. See Curator/TESTING.md."
        )

    _run_migrations(DATABASE_URL)
    connection = psycopg.connect(DATABASE_URL, autocommit=False)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture
def db_connection(migrated_connection):
    """Wrap each test in a ``SAVEPOINT`` on the session's migrated connection, rolled back on exit.

    Every test using this fixture (directly or via ``seeded_user_and_game``) therefore leaves the schema
    exactly as it found it, regardless of pass/fail/exception — including a deliberately-raised
    CHECK-constraint violation. ``force_rollback`` is what makes that case work: the violation leaves the
    transaction aborted but the exception is swallowed by ``pytest.raises``, so the savepoint has to be
    rolled back rather than released.
    """
    with migrated_connection.transaction(force_rollback=True):
        yield migrated_connection


@pytest.fixture
def seeded_user_and_game(db_connection):
    """Insert one ``app_users`` row and one ``games`` row, returning ``(identity_sub, game_id)``.

    Several tests below need a valid foreign-key target before they can reach the CHECK constraint they're
    actually testing (an insert that fails its foreign key never reaches the CHECK).
    """
    user_sub = str(uuid.uuid4())
    game_id = str(uuid.uuid4())
    with db_connection.cursor() as cur:
        cur.execute("INSERT INTO app_users (identity_sub) VALUES (%s)", (user_sub,))
        cur.execute(
            "INSERT INTO games (game_id, canonical_title, normalized_title) VALUES (%s, %s, %s)",
            (game_id, "Test Game", "test game"),
        )
    return user_sub, game_id


def test_migration_creates_all_expected_tables(db_connection):
    with db_connection.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")
        actual_tables = {row[0] for row in cur.fetchall()}
    assert actual_tables >= EXPECTED_TABLES


def test_the_two_psn_search_cache_tables_no_longer_exist(db_connection):
    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public' AND table_name IN (%s, %s)",
            ("psn_game_search_cache", "psn_player_search_cache"),
        )
        surviving = cur.fetchall()
    assert surviving == []


def test_the_two_never_populated_catalog_columns_no_longer_exist(db_connection):
    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' "
            "AND ((table_name = %s AND column_name = %s) OR (table_name = %s AND column_name = %s))",
            ("games", "search_names", "game_enrichment", "collection_tier"),
        )
        surviving = cur.fetchall()
    assert surviving == []


def test_collection_items_rejects_invalid_collection_status(db_connection, seeded_user_and_game):
    user_sub, game_id = seeded_user_and_game
    run_id = str(uuid.uuid4())
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_runs (run_id, identity_sub, spec_snapshot) VALUES (%s, %s, %s)",
            (run_id, user_sub, "{}"),
        )
    with pytest.raises(psycopg_errors.CheckViolation), db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_items (run_id, game_id, included, collection_status) VALUES (%s, %s, %s, %s)",
            (run_id, game_id, True, "Wrong"),
        )


def test_game_enrichment_genre_id_rejects_orphan_fk(db_connection, seeded_user_and_game):
    _user_sub, game_id = seeded_user_and_game
    with pytest.raises(psycopg_errors.ForeignKeyViolation), db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO game_enrichment (game_id, genre_id) VALUES (%s, %s)",
            (game_id, str(uuid.uuid4())),
        )


def test_user_consoles_rejects_a_platform_outside_the_platforms_table(db_connection, seeded_user_and_game):
    user_sub, _game_id = seeded_user_and_game
    with pytest.raises(psycopg_errors.ForeignKeyViolation), db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO user_consoles (identity_sub, name, platform, raw_capacity_gb) VALUES (%s, %s, %s, %s)",
            (user_sub, "Living Room", "NOT-A-PLATFORM", 825),
        )


def test_exclusion_rules_rejects_invalid_rule_type(db_connection):
    with pytest.raises(psycopg_errors.CheckViolation), db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO exclusion_rules (rule_type, pattern) VALUES (%s, %s)",
            ("bogus", "some-pattern"),
        )


def test_collection_definitions_rejects_invalid_visibility(db_connection, seeded_user_and_game):
    user_sub, _game_id = seeded_user_and_game
    with pytest.raises(psycopg_errors.CheckViolation), db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_definitions (identity_sub, name, kind, visibility) VALUES (%s, %s, %s, %s)",
            (user_sub, "Bad Visibility", "filter_list", "everyone"),
        )


def test_collection_definitions_share_slug_is_unique(db_connection, seeded_user_and_game):
    user_sub, _game_id = seeded_user_and_game
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_definitions (identity_sub, name, kind, share_slug) VALUES (%s, %s, %s, %s)",
            (user_sub, "First", "filter_list", "same-slug"),
        )
    with pytest.raises(psycopg_errors.UniqueViolation), db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_definitions (identity_sub, name, kind, share_slug) VALUES (%s, %s, %s, %s)",
            (user_sub, "Second", "filter_list", "same-slug"),
        )


def test_deleting_a_console_untargets_its_collections_rather_than_deleting_them(db_connection, seeded_user_and_game):
    user_sub, _game_id = seeded_user_and_game
    console_id = str(uuid.uuid4())
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO user_consoles (console_id, identity_sub, name, platform, raw_capacity_gb) "
            "VALUES (%s, %s, %s, %s, %s)",
            (console_id, user_sub, "Living room PS5", "PS5", 825),
        )
        cur.execute(
            "INSERT INTO collection_definitions (identity_sub, name, kind, share_slug, install_target_console_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING definition_id",
            (user_sub, "For the PS5", "filter_list", "slug-0052", console_id),
        )
        definition_id = cur.fetchone()[0]

        cur.execute("DELETE FROM user_consoles WHERE console_id = %s", (console_id,))

        cur.execute(
            "SELECT install_target_console_id FROM collection_definitions WHERE definition_id = %s",
            (definition_id,),
        )
        surviving = cur.fetchall()

    assert surviving == [(None,)]


def test_an_install_target_that_is_no_console_is_rejected_so_the_set_null_above_is_not_vacuous(
    db_connection, seeded_user_and_game
):
    user_sub, _game_id = seeded_user_and_game
    with pytest.raises(psycopg_errors.ForeignKeyViolation), db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_definitions (identity_sub, name, kind, share_slug, install_target_console_id) "
            "VALUES (%s, %s, %s, %s, %s)",
            (user_sub, "Aimed at nothing", "filter_list", "slug-0052-orphan", str(uuid.uuid4())),
        )


def test_the_real_item_projection_reports_install_state_against_the_target_console(db_connection, seeded_user_and_game):
    user_sub, installed_game = seeded_user_and_game
    console_id, absent_game, definition_id = str(uuid.uuid4()), str(uuid.uuid4()), None
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO games (game_id, canonical_title, normalized_title) VALUES (%s, %s, %s)",
            (absent_game, "Not Installed", "not installed"),
        )
        cur.execute(
            "INSERT INTO user_consoles (console_id, identity_sub, name, platform, raw_capacity_gb) "
            "VALUES (%s, %s, %s, %s, %s)",
            (console_id, user_sub, "Target PS5", "PS5", 825),
        )
        cur.execute(
            "INSERT INTO collection_definitions (identity_sub, name, kind, share_slug, "
            "install_target_console_id) VALUES (%s, %s, %s, %s, %s) RETURNING definition_id",
            (user_sub, "Aimed", "filter_list", f"slug-{uuid.uuid4()}", console_id),
        )
        definition_id = cur.fetchone()[0]
        cur.executemany(
            "INSERT INTO collection_definition_items (definition_id, game_id, rank) VALUES (%s, %s, %s)",
            [(definition_id, installed_game, 1), (definition_id, absent_game, 2)],
        )
        cur.execute(
            "INSERT INTO console_installs (console_id, game_id, installed) VALUES (%s, %s, true)",
            (console_id, installed_game),
        )

        cur.execute(
            f"SELECT {_ITEM_SELECT_COLUMNS} {_ITEM_BASE_FROM} WHERE cdi.definition_id = %s ORDER BY cdi.rank",
            (definition_id,),
        )
        items = [CollectionsRepository._to_item(row) for row in cur.fetchall()]

    assert [item.installed_on_target for item in items] == [True, False], (
        "the projection and the row-index mapping are asserted together here because every other test of "
        "this query builds its rows by hand, so a reordered SELECT stays green"
    )


def test_the_item_projection_reports_no_install_state_when_a_collection_targets_no_console(
    db_connection, seeded_user_and_game
):
    user_sub, game_id = seeded_user_and_game
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_definitions (identity_sub, name, kind, share_slug) "
            "VALUES (%s, %s, %s, %s) RETURNING definition_id",
            (user_sub, "Untargeted", "filter_list", f"slug-{uuid.uuid4()}"),
        )
        definition_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO collection_definition_items (definition_id, game_id, rank) VALUES (%s, %s, 1)",
            (definition_id, game_id),
        )
        cur.execute(
            f"SELECT {_ITEM_SELECT_COLUMNS} {_ITEM_BASE_FROM} WHERE cdi.definition_id = %s",
            (definition_id,),
        )
        items = [CollectionsRepository._to_item(row) for row in cur.fetchall()]

    assert [item.installed_on_target for item in items] == [None], (
        "None means the collection targets no console, which is a different fact from False"
    )


def test_game_measured_sizes_upserts_per_game_and_platform(db_connection, seeded_user_and_game):
    """WP13: global cache, not history -- a second contribution for the same (game_id, platform)
    overwrites the first rather than accumulating a row (the never-written `measured_sizes` table this
    replaced was the opposite shape; see migration 0025's own header comment)."""
    user_sub, game_id = seeded_user_and_game
    upsert_sql = (
        "INSERT INTO game_measured_sizes (game_id, platform, size_gb, recorded_by) VALUES (%s, %s, %s, %s) "
        "ON CONFLICT (game_id, platform) DO UPDATE SET "
        "size_gb = EXCLUDED.size_gb, recorded_by = EXCLUDED.recorded_by"
    )
    with db_connection.cursor() as cur:
        cur.execute(upsert_sql, (game_id, "PS5", 42.5, user_sub))
        cur.execute(upsert_sql, (game_id, "PS5", 50.0, user_sub))
        cur.execute(
            "SELECT count(*), max(size_gb) FROM game_measured_sizes WHERE game_id = %s AND platform = %s",
            (game_id, "PS5"),
        )
        count, size_gb = cur.fetchone()
    assert count == 1
    assert float(size_gb) == 50.0


def test_game_measured_sizes_recorded_by_survives_contributor_deletion(db_connection, seeded_user_and_game):
    """WP13: `recorded_by` is `ON DELETE SET NULL`, not `CASCADE` -- the measured size is global,
    contributed data other users' collections may already be sized against, so deleting the contributor's
    account must not silently delete it (see migration 0025's own header comment)."""
    user_sub, game_id = seeded_user_and_game
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO game_measured_sizes (game_id, platform, size_gb, recorded_by) VALUES (%s, %s, %s, %s)",
            (game_id, "PS5", 42.5, user_sub),
        )
        cur.execute("DELETE FROM app_users WHERE identity_sub = %s", (user_sub,))
        cur.execute(
            "SELECT size_gb, recorded_by FROM game_measured_sizes WHERE game_id = %s AND platform = %s",
            (game_id, "PS5"),
        )
        size_gb, recorded_by = cur.fetchone()
    assert float(size_gb) == 42.5
    assert recorded_by is None


def test_job_runs_rejects_invalid_status(db_connection):
    run_id = str(uuid.uuid4())
    with pytest.raises(psycopg_errors.CheckViolation), db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO job_runs (run_id, kind, status) VALUES (%s, %s, %s)",
            (run_id, "library_refresh", "bogus"),
        )


def test_job_runs_accepts_the_cancelled_status(db_connection):
    cancelled_run_id = str(uuid.uuid4())
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO job_runs (run_id, kind, status) VALUES (%s, %s, %s)",
            (cancelled_run_id, "enrichment", "cancelled"),
        )
        cur.execute("SELECT status FROM job_runs WHERE run_id = %s", (cancelled_run_id,))
        (status,) = cur.fetchone()
    assert status == "cancelled"


def test_the_python_platform_vocabulary_matches_the_platforms_table(db_connection):
    """``CONSOLE_PLATFORM_IDS`` is hand-written -- a ``Literal`` cannot be built at runtime -- so this is
    what stops it drifting from the reference table it narrows. Without it, a platform added by a
    migration surfaces as a 400 on a console the schema already accepts, and nothing goes red."""
    with db_connection.cursor() as cur:
        cur.execute("SELECT platform_id FROM platforms WHERE active ORDER BY sort_order")
        stored = tuple(row[0] for row in cur.fetchall())
    assert stored == CONSOLE_PLATFORM_IDS


def test_library_entry_platforms_rejects_a_platform_outside_the_platforms_table(db_connection, seeded_user_and_game):
    user_sub, game_id = seeded_user_and_game
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (identity_sub, game_id, source) VALUES (%s, %s, %s)",
            (user_sub, game_id, "manual"),
        )
    with pytest.raises(psycopg_errors.ForeignKeyViolation), db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entry_platforms (identity_sub, game_id, platform) VALUES (%s, %s, %s)",
            (user_sub, game_id, "NOT-A-PLATFORM"),
        )


def test_psn_catalog_cache_raw_defaults_to_an_empty_object_rather_than_null(db_connection):
    """A reader must never have to tell "no payload stored" from "payload stored and empty" -- both mean
    the row predates a walk that kept it. Same shape as ``entitlement_snapshots.raw``."""
    with db_connection.cursor() as cur:
        cur.execute("INSERT INTO psn_catalog_cache (title_id) VALUES (%s)", ("CUSA00207_00",))
        cur.execute("SELECT raw FROM psn_catalog_cache WHERE title_id = %s", ("CUSA00207_00",))
        (raw,) = cur.fetchone()
    assert raw == {}


def test_job_runs_accepts_the_abandoned_error_code(db_connection):
    reaped_run_id = str(uuid.uuid4())
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO job_runs (run_id, kind, status, error_code) VALUES (%s, %s, %s, %s)",
            (reaped_run_id, "library_refresh", "failed", "abandoned"),
        )
        cur.execute("SELECT error_code FROM job_runs WHERE run_id = %s", (reaped_run_id,))
        (error_code,) = cur.fetchone()
    assert error_code == "abandoned"


def test_job_runs_rejects_an_error_code_outside_the_closed_vocabulary(db_connection):
    uncoded_run_id = str(uuid.uuid4())
    unknown_error_code = str(uuid.uuid4())
    with pytest.raises(psycopg_errors.CheckViolation), db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO job_runs (run_id, kind, status, error_code) VALUES (%s, %s, %s, %s)",
            (uncoded_run_id, "library_refresh", "failed", unknown_error_code),
        )


def test_follows_rejects_self_follow(db_connection, seeded_user_and_game):
    user_sub, _game_id = seeded_user_and_game
    with pytest.raises(psycopg_errors.CheckViolation), db_connection.cursor() as cur:
        cur.execute("INSERT INTO follows (follower_sub, followed_sub) VALUES (%s, %s)", (user_sub, user_sub))


def test_follows_cascade_deletes_when_either_user_is_deleted(db_connection):
    follower_sub = str(uuid.uuid4())
    followed_sub = str(uuid.uuid4())
    with db_connection.cursor() as cur:
        cur.execute("INSERT INTO app_users (identity_sub) VALUES (%s), (%s)", (follower_sub, followed_sub))
        cur.execute("INSERT INTO follows (follower_sub, followed_sub) VALUES (%s, %s)", (follower_sub, followed_sub))
        cur.execute("DELETE FROM app_users WHERE identity_sub = %s", (follower_sub,))
        cur.execute(
            "SELECT count(*) FROM follows WHERE follower_sub = %s AND followed_sub = %s",
            (follower_sub, followed_sub),
        )
        (count,) = cur.fetchone()
    assert count == 0


def test_user_profiles_cascade_deletes_when_user_is_deleted(db_connection, seeded_user_and_game):
    user_sub, _game_id = seeded_user_and_game
    with db_connection.cursor() as cur:
        cur.execute("INSERT INTO user_profiles (identity_sub, is_public) VALUES (%s, %s)", (user_sub, True))
        cur.execute("DELETE FROM app_users WHERE identity_sub = %s", (user_sub,))
        cur.execute("SELECT count(*) FROM user_profiles WHERE identity_sub = %s", (user_sub,))
        (count,) = cur.fetchone()
    assert count == 0


def test_deleting_a_user_cascades_every_per_user_table(db_connection, seeded_user_and_game):
    """DELETE /me must wipe every per-user table via cascade -- the contract delete_user relies on.

    Eight of these foreign keys were declared without ON DELETE CASCADE in 0001_initial.sql, so this
    delete raised a ForeignKeyViolation for any user who had ingested entitlements or saved a
    collection. 0009_fix_delete_cascades.sql added them, plus the four child-chain cascades
    (entitlement_snapshots, collection_items, console_installs, collection_runs.definition_id) that
    would otherwise have failed one level down. storage_devices/storage_device_installs (0017) follow
    the same identity_sub-cascade, device_id-child-cascade shape from the start.
    """
    user_sub, game_id = seeded_user_and_game
    pull_id = str(uuid.uuid4())
    console_id = str(uuid.uuid4())
    device_id = str(uuid.uuid4())
    definition_id = str(uuid.uuid4())
    run_id = str(uuid.uuid4())
    job_run_id = str(uuid.uuid4())

    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO entitlement_pulls (pull_id, identity_sub, source, entry_count) VALUES (%s, %s, %s, %s)",
            (pull_id, user_sub, "curator-live", 1),
        )
        cur.execute(
            "INSERT INTO entitlement_snapshots "
            "(identity_sub, pull_id, entitlement_id, raw, first_seen_at, last_seen_at) "
            "VALUES (%s, %s, %s, %s, now(), now())",
            (user_sub, pull_id, "ent-1", "{}"),
        )
        cur.execute(
            "INSERT INTO library_entries (identity_sub, game_id, winning_entitlement_id) VALUES (%s, %s, %s)",
            (user_sub, game_id, "ent-1"),
        )
        cur.execute(
            "INSERT INTO library_exclusions (identity_sub, game_id, reason) VALUES (%s, %s, %s)",
            (user_sub, game_id, "not interested"),
        )
        cur.execute(
            "INSERT INTO user_consoles (console_id, identity_sub, name, platform, raw_capacity_gb) "
            "VALUES (%s, %s, %s, %s, %s)",
            (console_id, user_sub, "Living room PS5", "PS5", 800.0),
        )
        cur.execute("INSERT INTO console_installs (console_id, game_id) VALUES (%s, %s)", (console_id, game_id))
        cur.execute(
            "INSERT INTO storage_devices (device_id, identity_sub, console_id, name, kind, capacity_gb) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (device_id, user_sub, console_id, "Travel drive", "usb", 500.0),
        )
        cur.execute("INSERT INTO storage_device_installs (device_id, game_id) VALUES (%s, %s)", (device_id, game_id))
        cur.execute(
            "INSERT INTO game_measured_sizes (game_id, platform, size_gb, recorded_by) VALUES (%s, %s, %s, %s)",
            (game_id, "PS5", 50.0, user_sub),
        )
        cur.execute(
            "INSERT INTO collection_definitions (definition_id, identity_sub, name, kind) VALUES (%s, %s, %s, %s)",
            (definition_id, user_sub, "My RPGs", "filter_list"),
        )
        cur.execute(
            "INSERT INTO collection_definition_items (definition_id, game_id, rank) VALUES (%s, %s, %s)",
            (definition_id, game_id, 1),
        )
        cur.execute(
            "INSERT INTO collection_follows (follower_sub, definition_id) VALUES (%s, %s)",
            (user_sub, definition_id),
        )
        cur.execute(
            "INSERT INTO collection_runs (run_id, identity_sub, definition_id, spec_snapshot) VALUES (%s, %s, %s, %s)",
            (run_id, user_sub, definition_id, "{}"),
        )
        cur.execute(
            "INSERT INTO collection_items (run_id, game_id, included) VALUES (%s, %s, %s)",
            (run_id, game_id, True),
        )
        cur.execute(
            "INSERT INTO job_runs (run_id, kind, identity_sub) VALUES (%s, %s, %s)",
            (job_run_id, "library_refresh", user_sub),
        )
        cur.execute(
            "INSERT INTO account_action_log (identity_sub, action) VALUES (%s, %s)",
            (user_sub, "account_deleted"),
        )

        cur.execute("DELETE FROM app_users WHERE identity_sub = %s", (user_sub,))

        for table in (
            "entitlement_pulls",
            "library_entries",
            "library_exclusions",
            "user_consoles",
            "storage_devices",
            "collection_definitions",
            "collection_runs",
            "job_runs",
        ):
            cur.execute(f"SELECT count(*) FROM {table} WHERE identity_sub = %s", (user_sub,))
            (count,) = cur.fetchone()
            assert count == 0, f"{table} still has rows for the deleted user"

        cur.execute(
            "SELECT recorded_by FROM game_measured_sizes WHERE game_id = %s AND platform = %s", (game_id, "PS5")
        )
        assert cur.fetchone()[0] is None

        cur.execute("SELECT count(*) FROM entitlement_snapshots WHERE pull_id = %s", (pull_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM collection_items WHERE run_id = %s", (run_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM collection_definition_items WHERE definition_id = %s", (definition_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM console_installs WHERE console_id = %s", (console_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM storage_device_installs WHERE device_id = %s", (device_id,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM collection_follows WHERE definition_id = %s", (definition_id,))
        assert cur.fetchone()[0] == 0

        cur.execute("SELECT count(*) FROM account_action_log WHERE identity_sub = %s", (user_sub,))
        assert cur.fetchone()[0] == 1


def test_deleting_a_console_detaches_its_storage_device_rather_than_deleting_it(db_connection, seeded_user_and_game):
    """A swappable drive is not destroyed by unplugging it -- deleting a console must SET NULL on
    storage_devices.console_id, not cascade-delete the device (0017), matching the physical reality a
    device outlives whatever it was plugged into.
    """
    user_sub, game_id = seeded_user_and_game
    console_id = str(uuid.uuid4())
    device_id = str(uuid.uuid4())

    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO user_consoles (console_id, identity_sub, name, platform, raw_capacity_gb) "
            "VALUES (%s, %s, %s, %s, %s)",
            (console_id, user_sub, "Living room PS5", "PS5", 800.0),
        )
        cur.execute(
            "INSERT INTO storage_devices (device_id, identity_sub, console_id, name, kind, capacity_gb) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (device_id, user_sub, console_id, "Travel drive", "usb", 500.0),
        )
        cur.execute("INSERT INTO storage_device_installs (device_id, game_id) VALUES (%s, %s)", (device_id, game_id))

        cur.execute("DELETE FROM user_consoles WHERE console_id = %s", (console_id,))

        cur.execute("SELECT console_id FROM storage_devices WHERE device_id = %s", (device_id,))
        row = cur.fetchone()
        assert row is not None, "the device itself must survive its console being deleted"
        assert row[0] is None, "the device must become unattached, not still point at the deleted console"

        cur.execute(
            "SELECT installed FROM storage_device_installs WHERE device_id = %s AND game_id = %s", (device_id, game_id)
        )
        assert cur.fetchone() is not None


def test_library_exclusions_cascade_but_shared_catalog_survives(db_connection, seeded_user_and_game):
    """A user delete must never touch the shared, identity_sub-free catalog tables."""
    user_sub, game_id = seeded_user_and_game
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO library_exclusions (identity_sub, game_id, reason) VALUES (%s, %s, %s)",
            (user_sub, game_id, "not interested"),
        )
        cur.execute("DELETE FROM app_users WHERE identity_sub = %s", (user_sub,))
        cur.execute("SELECT count(*) FROM games WHERE game_id = %s", (game_id,))
        assert cur.fetchone()[0] == 1


def test_is_active_is_per_user_and_defaults_to_true(db_connection, seeded_user_and_game):
    """One user's lapsed entitlement must never change another user's view of the same game.

    This is the layering ``0012_library_entry_active_state.sql`` exists to enforce: is_active lives on
    library_entries, so the same shared ``games`` row is simultaneously active for one user and inactive
    for another. A column on ``games`` or ``game_enrichment`` would make one person's expired PS Plus
    subscription reclassify the title for everybody.
    """
    user_a, game_id = seeded_user_and_game
    user_b = str(uuid.uuid4())
    with db_connection.cursor() as cur:
        cur.execute("INSERT INTO app_users (identity_sub) VALUES (%s)", (user_b,))
        cur.execute(
            "INSERT INTO library_entries (identity_sub, game_id, is_active, winning_entitlement_id) "
            "VALUES (%s, %s, false, 'ent-a')",
            (user_a, game_id),
        )
        cur.execute(
            "INSERT INTO library_entries (identity_sub, game_id, winning_entitlement_id) VALUES (%s, %s, %s)",
            (user_b, game_id, "ent-b"),
        )

        cur.execute(
            "SELECT identity_sub, is_active FROM library_entries WHERE game_id = %s ORDER BY is_active",
            (game_id,),
        )
        rows = cur.fetchall()

    assert [(str(row[0]), row[1]) for row in rows] == [(user_a, False), (user_b, True)]


def test_set_trophy_match_accepts_a_no_match_result(db_connection, seeded_user_and_game):
    """``curator.library.repository.LibraryRepository.set_trophy_match`` must accept ``percent_completed=None``.

    Regression test for a bug only a real Postgres connection can catch: this method's query has a
    ``CASE WHEN %s IS NULL THEN ... ELSE now() END`` on ``trophy_progress_fetched_at``, referencing the
    same ``percent_completed`` value a second time. That second parameter occurrence is never assigned to
    or compared against a typed column, so with no cast Postgres cannot infer its type and psycopg raises
    ``IndeterminateDatatype: could not determine data type of parameter $4`` -- but only when the value
    actually sent is NULL, so unit tests against a hand-written fake (which never sends SQL anywhere) pass
    regardless, and this class of bug reaches a real database undetected. It reached this one: this exact
    call, from a real local library refresh, is what surfaced it -- the "no confident trophy match" path
    (``np_communication_id=None``) is the common case on a first pull, so it was also the first path hit.

    The query text here intentionally mirrors ``set_trophy_match`` rather than calling it, since that
    method is async (``psycopg_pool.AsyncConnectionPool``) and this module's fixtures are synchronous --
    keep the two in sync if either changes.
    """
    user_sub, game_id = seeded_user_and_game
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (identity_sub, game_id, winning_entitlement_id) VALUES (%s, %s, %s)",
            (user_sub, game_id, "ent-1"),
        )

        cur.execute(
            """
            UPDATE library_entries
            SET np_communication_id = %s,
                trophy_match_method = %s,
                trophy_match_attempted_at = now(),
                trophy_percent_completed = %s,
                trophy_progress_fetched_at = CASE WHEN %s::smallint IS NULL
                    THEN trophy_progress_fetched_at ELSE now() END
            WHERE identity_sub = %s AND game_id = %s
            """,
            (None, None, None, None, user_sub, game_id),
        )

        cur.execute(
            "SELECT np_communication_id, trophy_percent_completed, trophy_progress_fetched_at, "
            "trophy_match_attempted_at IS NOT NULL FROM library_entries WHERE identity_sub = %s AND game_id = %s",
            (user_sub, game_id),
        )
        row = cur.fetchone()

    assert row == (None, None, None, True)


def test_set_trophy_match_accepts_a_confident_match_with_progress(db_connection, seeded_user_and_game):
    """Same query as above, the other branch: a real match with a known completion percentage.

    Covers the ``CASE`` taking its ELSE arm (stamping ``trophy_progress_fetched_at``) so the fix for the
    NULL branch above cannot have broken this one.
    """
    user_sub, game_id = seeded_user_and_game
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO library_entries (identity_sub, game_id, winning_entitlement_id) VALUES (%s, %s, %s)",
            (user_sub, game_id, "ent-1"),
        )

        cur.execute(
            """
            UPDATE library_entries
            SET np_communication_id = %s,
                trophy_match_method = %s,
                trophy_match_attempted_at = now(),
                trophy_percent_completed = %s,
                trophy_progress_fetched_at = CASE WHEN %s::smallint IS NULL
                    THEN trophy_progress_fetched_at ELSE now() END
            WHERE identity_sub = %s AND game_id = %s
            """,
            ("NPWR12345_00", "fuzzy", 42, 42, user_sub, game_id),
        )

        cur.execute(
            "SELECT np_communication_id, trophy_match_method, trophy_percent_completed, "
            "trophy_progress_fetched_at IS NOT NULL FROM library_entries WHERE identity_sub = %s AND game_id = %s",
            (user_sub, game_id),
        )
        row = cur.fetchone()

    assert row == ("NPWR12345_00", "fuzzy", 42, True)


def test_account_action_log_accepts_followed_and_unfollowed_actions(db_connection, seeded_user_and_game):
    user_sub, _game_id = seeded_user_and_game
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO account_action_log (identity_sub, action, detail) VALUES (%s, %s, %s)",
            (user_sub, "followed", "some-other-sub"),
        )
        cur.execute(
            "INSERT INTO account_action_log (identity_sub, action, detail) VALUES (%s, %s, %s)",
            (user_sub, "unfollowed", "some-other-sub"),
        )
        cur.execute("SELECT count(*) FROM account_action_log WHERE identity_sub = %s", (user_sub,))
        (count,) = cur.fetchone()
    assert count == 2


def test_account_action_log_accepts_enrichment_key_rejected_action(db_connection, seeded_user_and_game):
    user_sub, _game_id = seeded_user_and_game
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO account_action_log (identity_sub, action, detail) VALUES (%s, %s, %s)",
            (user_sub, "enrichment_key_rejected", "rawg"),
        )
        cur.execute("SELECT count(*) FROM account_action_log WHERE identity_sub = %s", (user_sub,))
        (count,) = cur.fetchone()
    assert count == 1


def test_user_enrichment_keys_rejected_at_columns_round_trip(db_connection, seeded_user_and_game):
    user_sub, _game_id = seeded_user_and_game
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO user_enrichment_keys (identity_sub, rawg_api_key_enc, rawg_added_at) VALUES (%s, %s, now())",
            (user_sub, b"enc"),
        )
        cur.execute("UPDATE user_enrichment_keys SET rawg_key_rejected_at = now() WHERE identity_sub = %s", (user_sub,))
        cur.execute(
            "SELECT rawg_key_rejected_at, opencritic_key_rejected_at FROM user_enrichment_keys WHERE identity_sub = %s",
            (user_sub,),
        )
        rawg_rejected_at, opencritic_rejected_at = cur.fetchone()
    assert rawg_rejected_at is not None
    assert opencritic_rejected_at is None


def test_curation_rule_tables_are_seeded(db_connection):
    with db_connection.cursor() as cur:
        cur.execute("SELECT count(*) FROM franchise_rules")
        (franchise_rule_count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM publisher_tiers")
        (publisher_tier_count,) = cur.fetchone()
        cur.execute("SELECT count(*) FROM genres")
        (genre_count,) = cur.fetchone()
        cur.execute("SELECT franchise FROM franchise_rules WHERE pattern = %s", ("call of duty",))
        (call_of_duty_franchise,) = cur.fetchone()
    assert franchise_rule_count > 0
    assert publisher_tier_count > 0
    assert genre_count > 0
    assert call_of_duty_franchise == "Call of Duty"


def test_no_email_or_npsso_columns_anywhere(db_connection):
    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND (column_name ILIKE %s OR column_name ILIKE %s)",
            ("%email%", "%npsso%"),
        )
        offending = cur.fetchall()
    assert offending == []


def test_collection_definitions_filter_predicate_defaults_to_null(db_connection, seeded_user_and_game):
    user_sub, _game_id = seeded_user_and_game
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_definitions (identity_sub, name, kind) VALUES (%s, %s, %s)",
            (user_sub, "Handpicked", "filter_list"),
        )
        cur.execute("SELECT filter_predicate FROM collection_definitions WHERE identity_sub = %s", (user_sub,))
        (filter_predicate,) = cur.fetchone()
    assert filter_predicate is None


def test_collection_definitions_filter_predicate_round_trips_as_jsonb(db_connection, seeded_user_and_game):
    """WP8: a saved collection's OR-capable predicate tree (curator.collections.filter_predicate) --
    stored as JSONB, not normalized predicate tables, since genre_filter/min_score/aaa_tier_filter are
    already provenance-only and never re-evaluated to decide membership (see this table's original
    migration header comment)."""
    user_sub, _game_id = seeded_user_and_game
    predicate = {
        "op": "or",
        "nodes": [
            {"op": "genre_in", "values": ["RPG", "Adventure"]},
            {"op": "and", "nodes": [{"op": "genre_in", "values": ["Action"]}, {"op": "tier_in", "values": ["Indie"]}]},
        ],
    }
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO collection_definitions (identity_sub, name, kind, filter_predicate) VALUES (%s, %s, %s, %s)",
            (user_sub, "Criterion-ish", "filter_list", json.dumps(predicate)),
        )
        cur.execute("SELECT filter_predicate FROM collection_definitions WHERE identity_sub = %s", (user_sub,))
        (filter_predicate,) = cur.fetchone()
    assert filter_predicate == predicate


TYPOGRAPHIC_APOSTROPHE = chr(0x2019)

SECOND_TYPOGRAPHIC_APOSTROPHE = chr(0x2018)

ASCII_APOSTROPHE = chr(0x27)

REMATCH_MIGRATION = MIGRATIONS_DIR / "0050_rematch_typographic_apostrophe_keys.sql"


def apply_rematch(cur):
    """Run migration 0050 against the current savepoint.

    The session fixture already applied it once over an empty database, where every statement was a no-op.
    Re-running it here against seeded rows is what exercises it, and re-running it a second time is what
    proves the idempotence its header claims -- the migration drops its own helper function, so each
    application starts from the same state the deploy job starts from.
    """
    cur.execute(REMATCH_MIGRATION.read_text(encoding="utf-8"))


def seed_possessive_game(cur, *, apostrophe, rawg_enriched, opencritic_enriched, attempted):
    """Insert one ``games`` row whose title is a possessive spelled with ``apostrophe``, plus its
    enrichment row.

    :returns: the new ``game_id``.
    """
    game_id = str(uuid.uuid4())
    title = f"Studio{uuid.uuid4().hex[:8]}{apostrophe}s Adventure"
    cur.execute(
        "INSERT INTO games (game_id, canonical_title, normalized_title) VALUES (%s, %s, %s)",
        (game_id, title, title.lower()),
    )
    cur.execute(
        "INSERT INTO game_enrichment (game_id, rawg_enriched, opencritic_enriched, rawg_attempted_at) "
        "VALUES (%s, %s, %s, %s)",
        (game_id, rawg_enriched, opencritic_enriched, "2026-08-01T00:00:00+00:00" if attempted else None),
    )
    return game_id


def rawg_payload(marker):
    return json.dumps({"slug": marker})


def test_rematch_rekeys_a_stale_rawg_cache_row_and_keeps_its_payload(db_connection):
    stale_key = f"studio{uuid.uuid4().hex[:8]}{TYPOGRAPHIC_APOSTROPHE}s adventure"
    fresh_key = stale_key.replace(TYPOGRAPHIC_APOSTROPHE, ASCII_APOSTROPHE)
    rawg_game_id = uuid.uuid4().int % 1_000_000
    payload = rawg_payload(uuid.uuid4().hex)
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO rawg_cache (normalized_title, rawg_game_id, raw) VALUES (%s, %s, %s::jsonb)",
            (stale_key, rawg_game_id, payload),
        )
        apply_rematch(cur)
        cur.execute(
            "SELECT normalized_title, rawg_game_id, raw FROM rawg_cache WHERE normalized_title IN (%s, %s)",
            (stale_key, fresh_key),
        )
        surviving = cur.fetchall()
    assert surviving == [(fresh_key, rawg_game_id, json.loads(payload))]


def test_rematch_leaves_an_already_normalized_rawg_cache_row_untouched(db_connection):
    fresh_key = f"studio{uuid.uuid4().hex[:8]}'s adventure"
    rawg_game_id = uuid.uuid4().int % 1_000_000
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO rawg_cache (normalized_title, rawg_game_id, raw) VALUES (%s, %s, %s::jsonb)",
            (fresh_key, rawg_game_id, rawg_payload(uuid.uuid4().hex)),
        )
        cur.execute("SELECT fetched_at FROM rawg_cache WHERE normalized_title = %s", (fresh_key,))
        (before,) = cur.fetchone()
        apply_rematch(cur)
        cur.execute(
            "SELECT normalized_title, rawg_game_id, fetched_at FROM rawg_cache WHERE normalized_title = %s",
            (fresh_key,),
        )
        after = cur.fetchone()
    assert after == (fresh_key, rawg_game_id, before)


def test_rematch_lets_a_colliding_stale_positive_replace_the_cached_miss_it_lands_on(db_connection):
    stale_key = f"studio{uuid.uuid4().hex[:8]}{TYPOGRAPHIC_APOSTROPHE}s adventure"
    fresh_key = stale_key.replace(TYPOGRAPHIC_APOSTROPHE, ASCII_APOSTROPHE)
    rawg_game_id = uuid.uuid4().int % 1_000_000
    payload = rawg_payload(uuid.uuid4().hex)
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO rawg_cache (normalized_title, rawg_game_id, raw) VALUES (%s, NULL, NULL)", (fresh_key,)
        )
        cur.execute(
            "INSERT INTO rawg_cache (normalized_title, rawg_game_id, raw) VALUES (%s, %s, %s::jsonb)",
            (stale_key, rawg_game_id, payload),
        )
        apply_rematch(cur)
        cur.execute(
            "SELECT normalized_title, rawg_game_id, raw FROM rawg_cache WHERE normalized_title IN (%s, %s)",
            (stale_key, fresh_key),
        )
        surviving = cur.fetchall()
    assert surviving == [(fresh_key, rawg_game_id, json.loads(payload))]


def test_rematch_never_lets_a_stale_cached_miss_overwrite_the_payload_it_lands_on(db_connection):
    stale_key = f"studio{uuid.uuid4().hex[:8]}{TYPOGRAPHIC_APOSTROPHE}s adventure"
    fresh_key = stale_key.replace(TYPOGRAPHIC_APOSTROPHE, ASCII_APOSTROPHE)
    rawg_game_id = uuid.uuid4().int % 1_000_000
    payload = rawg_payload(uuid.uuid4().hex)
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO rawg_cache (normalized_title, rawg_game_id, raw) VALUES (%s, %s, %s::jsonb)",
            (fresh_key, rawg_game_id, payload),
        )
        cur.execute(
            "INSERT INTO rawg_cache (normalized_title, rawg_game_id, raw) VALUES (%s, NULL, NULL)", (stale_key,)
        )
        apply_rematch(cur)
        cur.execute(
            "SELECT normalized_title, rawg_game_id, raw FROM rawg_cache WHERE normalized_title IN (%s, %s)",
            (stale_key, fresh_key),
        )
        surviving = cur.fetchall()
    assert surviving == [(fresh_key, rawg_game_id, json.loads(payload))]


def test_rematch_folds_two_stale_spellings_of_one_title_onto_a_single_surviving_row(db_connection):
    """Two typographic spellings of the same title fold to the same fresh key. Re-keying both in place
    would collide on rawg_cache's primary key and abort the whole migration -- and a migration that fails
    is never recorded in schema_migrations, so every later deploy would retry and fail the same way."""
    base = f"studio{uuid.uuid4().hex[:8]}"
    right_single_quote_key = f"{base}{TYPOGRAPHIC_APOSTROPHE}s adventure"
    left_single_quote_key = f"{base}{SECOND_TYPOGRAPHIC_APOSTROPHE}s adventure"
    fresh_key = f"{base}{ASCII_APOSTROPHE}s adventure"
    rawg_game_id = uuid.uuid4().int % 1_000_000
    payload = rawg_payload(uuid.uuid4().hex)
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO rawg_cache (normalized_title, rawg_game_id, raw) VALUES (%s, NULL, NULL)",
            (left_single_quote_key,),
        )
        cur.execute(
            "INSERT INTO rawg_cache (normalized_title, rawg_game_id, raw) VALUES (%s, %s, %s::jsonb)",
            (right_single_quote_key, rawg_game_id, payload),
        )
        apply_rematch(cur)
        cur.execute(
            "SELECT normalized_title, rawg_game_id, raw FROM rawg_cache WHERE normalized_title IN (%s, %s, %s)",
            (right_single_quote_key, left_single_quote_key, fresh_key),
        )
        surviving = cur.fetchall()
    assert surviving == [(fresh_key, rawg_game_id, json.loads(payload))]


def test_rematch_clears_the_tombstone_only_where_a_provider_actually_missed(db_connection):
    with db_connection.cursor() as cur:
        missed_id = seed_possessive_game(
            cur, apostrophe=TYPOGRAPHIC_APOSTROPHE, rawg_enriched=True, opencritic_enriched=False, attempted=True
        )
        matched_id = seed_possessive_game(
            cur, apostrophe=TYPOGRAPHIC_APOSTROPHE, rawg_enriched=True, opencritic_enriched=True, attempted=True
        )
        ascii_id = seed_possessive_game(
            cur, apostrophe=ASCII_APOSTROPHE, rawg_enriched=True, opencritic_enriched=False, attempted=True
        )
        apply_rematch(cur)
        cur.execute(
            "SELECT game_id FROM game_enrichment WHERE rawg_attempted_at IS NULL AND game_id IN (%s, %s, %s)",
            (missed_id, matched_id, ascii_id),
        )
        re_enrolled = [str(row[0]) for row in cur.fetchall()]
    assert re_enrolled == [missed_id]


def test_rematch_is_a_no_op_on_a_second_application(db_connection):
    stale_key = f"studio{uuid.uuid4().hex[:8]}{TYPOGRAPHIC_APOSTROPHE}s adventure"
    fresh_key = stale_key.replace(TYPOGRAPHIC_APOSTROPHE, ASCII_APOSTROPHE)
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO rawg_cache (normalized_title, rawg_game_id, raw) VALUES (%s, %s, %s::jsonb)",
            (stale_key, uuid.uuid4().int % 1_000_000, rawg_payload(uuid.uuid4().hex)),
        )
        apply_rematch(cur)
        cur.execute(
            "SELECT normalized_title, rawg_game_id, raw, fetched_at FROM rawg_cache WHERE normalized_title = %s",
            (fresh_key,),
        )
        after_first = cur.fetchone()
        apply_rematch(cur)
        cur.execute(
            "SELECT normalized_title, rawg_game_id, raw, fetched_at FROM rawg_cache WHERE normalized_title = %s",
            (fresh_key,),
        )
        after_second = cur.fetchone()
    assert after_first is not None
    assert after_second == after_first
