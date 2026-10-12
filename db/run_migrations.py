"""Idempotent migration runner: applies every ``db/migrations/*.sql`` file not yet applied.

Tracks applied filenames in a ``schema_migrations`` table so this can run safely on every deploy
without re-executing an already-applied file -- the migrations use plain ``CREATE TABLE``/``ALTER
TABLE``, not ``IF NOT EXISTS`` guards, so a rerun would fail outright rather than no-op.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
DATABASE_URL_VARIABLE = "CURATOR_MIGRATIONS_DATABASE_URL"


def database_url_from(arguments: Sequence[str], environment: Mapping[str, str]) -> str | None:
    """The database to migrate: the one command-line argument, else :data:`DATABASE_URL_VARIABLE` when there is
    no argument, so a caller holding the password can keep it off the process's command line. ``None`` when
    neither names one.

    :param arguments: The command-line arguments after the script name.
    :param environment: The process environment.
    """
    if len(arguments) == 1:
        return arguments[0]
    if arguments:
        return None
    value = environment.get(DATABASE_URL_VARIABLE)
    return value if value is not None and value.strip() else None


def run_migrations(database_url: str) -> None:
    """Apply every migration file in :data:`MIGRATIONS_DIR` not already recorded as applied.

    :param database_url: The ``postgresql://`` connection string to migrate.
    """
    with psycopg.connect(database_url, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
        )
        cur.execute("SELECT filename FROM schema_migrations")
        applied = {row[0] for row in cur.fetchall()}

        for migration_file in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if migration_file.name in applied:
                print(f"skip (already applied): {migration_file.name}")
                continue
            print(f"applying: {migration_file.name}")
            with conn.transaction():
                cur.execute(migration_file.read_text(encoding="utf-8"))
                cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (migration_file.name,))
            print(f"applied: {migration_file.name}")


def main() -> None:
    database_url = database_url_from(sys.argv[1:], os.environ)
    if database_url is None:
        print(f"Usage: python run_migrations.py <database_url>, or set {DATABASE_URL_VARIABLE}", file=sys.stderr)
        raise SystemExit(2)
    run_migrations(database_url)


if __name__ == "__main__":
    main()
