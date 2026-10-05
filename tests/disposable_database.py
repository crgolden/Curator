"""The suffixes that mark a PostgreSQL database as disposable, and the predicate every committing suite reads."""

from __future__ import annotations

TEST_DATABASE_SUFFIX = "_test"
TRIAGE_DATABASE_SUFFIX = "_triage"


def is_disposable_database(database: str | None) -> bool:
    """Whether a committing suite may migrate and sweep ``database``: its name ends in ``_test`` or ``_triage``."""
    return database is not None and database.endswith((TEST_DATABASE_SUFFIX, TRIAGE_DATABASE_SUFFIX))
