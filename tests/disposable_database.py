from __future__ import annotations

TEST_DATABASE_SUFFIX = "_test"
TRIAGE_DATABASE_SUFFIX = "_triage"


def is_disposable_database(database: str | None) -> bool:
    return database is not None and database.casefold().endswith((TEST_DATABASE_SUFFIX, TRIAGE_DATABASE_SUFFIX))
