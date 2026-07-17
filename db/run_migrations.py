"""Idempotent migration runner: applies every ``db/migrations/*.sql`` file not yet applied.

Tracks applied filenames in a ``schema_migrations`` table so this can run safely on every deploy
without re-executing an already-applied file -- the migrations use plain ``CREATE TABLE``/``ALTER
TABLE``, not ``IF NOT EXISTS`` guards, so a rerun would fail outright rather than no-op.
"""

from __future__ import annotations

import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


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
    if len(sys.argv) != 2:
        print("Usage: python run_migrations.py <database_url>", file=sys.stderr)
        raise SystemExit(2)
    run_migrations(sys.argv[1])


if __name__ == "__main__":
    main()
