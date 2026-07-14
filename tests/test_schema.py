"""Integration tests for ``db/migrations/0001_initial.sql``, applied to a **real** PostgreSQL instance.

These are the only tests in this suite that touch a live database. They are gated on the
``CURATOR_TEST_DATABASE_URL`` environment variable via a module-level ``pytest.mark.skipif`` — when it is
unset (the default in CI and any plain local ``pytest`` run), every test in this module is skipped, not
run against a fake or an in-memory substitute. When you do set it, **point it at a disposable, throwaway
database created solely for this purpose** — never a shared or production database. Each test applies the
full migration and every insert inside one transaction, then rolls that transaction back in teardown, so a
correctly-configured disposable database is left exactly as it started; nothing here ever commits.

Example (PowerShell), using a scratch database on a local/dev PostgreSQL instance you control:

    $env:CURATOR_TEST_DATABASE_URL = "postgresql://postgres@localhost:5432/curator_schema_test_scratch"
    python -m pytest tests/test_schema.py -q
"""

from __future__ import annotations

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

MIGRATION_PATH = Path(__file__).resolve().parent.parent / "db" / "migrations" / "0001_initial.sql"

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
    "measured_sizes",
    "collection_definitions",
    "collection_runs",
    "collection_items",
    "console_installs",
    "job_runs",
}


@pytest.fixture
def db_connection():
    """Open a connection, apply the full migration inside one transaction, and roll it all back on exit.

    Every test using this fixture (directly or via ``seeded_user_and_game``) therefore leaves the target
    database exactly as it found it, regardless of pass/fail/exception — including a deliberately-raised
    CHECK-constraint violation, which only aborts the current transaction, not the connection's ability to
    be rolled back.
    """
    connection = psycopg.connect(DATABASE_URL, autocommit=False)
    migration_sql = MIGRATION_PATH.read_text(encoding="utf-8")
    with connection.cursor() as cur:
        cur.execute(migration_sql)
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


def test_measured_sizes_retains_history_across_measured_at(db_connection, seeded_user_and_game):
    user_sub, game_id = seeded_user_and_game
    with db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO measured_sizes (identity_sub, game_id, platform, size_gb, measured_at) "
            "VALUES (%s, %s, %s, %s, now())",
            (user_sub, game_id, "PS5", 42.5),
        )
        cur.execute(
            "INSERT INTO measured_sizes (identity_sub, game_id, platform, size_gb, measured_at) "
            "VALUES (%s, %s, %s, %s, now() + interval '1 second')",
            (user_sub, game_id, "PS5", 50.0),
        )
        cur.execute(
            "SELECT count(*) FROM measured_sizes WHERE identity_sub = %s AND game_id = %s",
            (user_sub, game_id),
        )
        (count,) = cur.fetchone()
    assert count == 2


def test_job_runs_rejects_invalid_status(db_connection):
    run_id = str(uuid.uuid4())
    with pytest.raises(psycopg_errors.CheckViolation), db_connection.cursor() as cur:
        cur.execute(
            "INSERT INTO job_runs (run_id, kind, status) VALUES (%s, %s, %s)",
            (run_id, "library_refresh", "bogus"),
        )


def test_no_email_or_npsso_columns_anywhere(db_connection):
    with db_connection.cursor() as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = 'public' AND (column_name ILIKE %s OR column_name ILIKE %s)",
            ("%email%", "%npsso%"),
        )
        offending = cur.fetchall()
    assert offending == []
