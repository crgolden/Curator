"""Integration tests for every ``db/migrations/*.sql`` file, applied in order to a **real** PostgreSQL
instance.

These are the only tests in this suite that touch a live database. They are gated on the
``CURATOR_TEST_DATABASE_URL`` environment variable via a module-level ``pytest.mark.skipif`` — when it is
unset (the default in CI and any plain local ``pytest`` run), every test in this module is skipped, not
run against a fake or an in-memory substitute. When you do set it, **point it at a disposable, throwaway
database created solely for this purpose** — never a shared or production database. Each test applies
every migration file (in filename order) and every insert inside one transaction, then rolls that
transaction back in teardown, so a correctly-configured disposable database is left exactly as it started;
nothing here ever commits.

Example (PowerShell), using a scratch database on a local/dev PostgreSQL instance you control:

    $env:CURATOR_TEST_DATABASE_URL = "postgresql://postgres@localhost:5432/curator_schema_test_scratch"
    python -m pytest tests/test_schema.py -q
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest
from psycopg import errors as psycopg_errors

DATABASE_URL = os.environ.get("CURATOR_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason=(
        "CURATOR_TEST_DATABASE_URL is not set; schema integration tests only run against an explicitly "
        "configured disposable PostgreSQL database. See this module's docstring."
    ),
)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "db" / "migrations"

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
    "psn_game_search_cache",
    "psn_player_search_cache",
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


@pytest.fixture
def db_connection():
    """Open a connection, apply every migration file (in filename order) inside one transaction, and roll
    it all back on exit.

    Every test using this fixture (directly or via ``seeded_user_and_game``) therefore leaves the target
    database exactly as it found it, regardless of pass/fail/exception — including a deliberately-raised
    CHECK-constraint violation, which only aborts the current transaction, not the connection's ability to
    be rolled back.
    """
    connection = psycopg.connect(DATABASE_URL, autocommit=False)
    with connection.cursor() as cur:
        for migration_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            cur.execute(migration_file.read_text(encoding="utf-8"))
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


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


def test_user_consoles_rejects_invalid_platform(db_connection, seeded_user_and_game):
    user_sub, _game_id = seeded_user_and_game
    with pytest.raises(psycopg_errors.CheckViolation), db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO user_consoles (identity_sub, name, platform, raw_capacity_gb) VALUES (%s, %s, %s, %s)",
            (user_sub, "Living Room", "PS3", 825),
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
            "INSERT INTO entitlement_snapshots (pull_id, entitlement_id, raw) VALUES (%s, %s, %s)",
            (pull_id, "ent-1", "{}"),
        )
        cur.execute("INSERT INTO library_entries (identity_sub, game_id) VALUES (%s, %s)", (user_sub, game_id))
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
            "INSERT INTO library_entries (identity_sub, game_id, is_active) VALUES (%s, %s, false)",
            (user_a, game_id),
        )
        cur.execute("INSERT INTO library_entries (identity_sub, game_id) VALUES (%s, %s)", (user_b, game_id))

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
        cur.execute("INSERT INTO library_entries (identity_sub, game_id) VALUES (%s, %s)", (user_sub, game_id))

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
        cur.execute("INSERT INTO library_entries (identity_sub, game_id) VALUES (%s, %s)", (user_sub, game_id))

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
