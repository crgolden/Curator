"""Which database ``db/run_migrations.py`` migrates: the argument the deploy job passes, or the environment
variable the triage gate sets so the password stays off its command line."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from test_values import lowercase_token, new_blank

RUN_MIGRATIONS_PATH = Path(__file__).resolve().parent.parent / "db" / "run_migrations.py"


def _load_run_migrations_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("curator_db_run_migrations_arguments", RUN_MIGRATIONS_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


RUN_MIGRATIONS = _load_run_migrations_module()


def test_the_argument_names_the_database_even_when_the_variable_is_set() -> None:
    argument = lowercase_token()

    resolved = RUN_MIGRATIONS.database_url_from([argument], {RUN_MIGRATIONS.DATABASE_URL_VARIABLE: lowercase_token()})

    assert resolved == argument


def test_with_no_argument_the_variable_names_the_database() -> None:
    variable = lowercase_token()

    resolved = RUN_MIGRATIONS.database_url_from([], {RUN_MIGRATIONS.DATABASE_URL_VARIABLE: variable})

    assert resolved == variable


def test_with_no_argument_and_no_variable_there_is_no_database() -> None:
    assert RUN_MIGRATIONS.database_url_from([], {}) is None


def test_a_blank_variable_names_no_database() -> None:
    assert RUN_MIGRATIONS.database_url_from([], {RUN_MIGRATIONS.DATABASE_URL_VARIABLE: new_blank()}) is None


def test_two_arguments_name_no_database() -> None:
    assert RUN_MIGRATIONS.database_url_from([lowercase_token(), lowercase_token()], {}) is None


class _RecordingCursor:
    def __init__(self, statements: list[tuple[str, object]], applied: list[str]) -> None:
        self._statements = statements
        self._applied = applied

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, statement: str, parameters: object = None) -> None:
        self._statements.append((statement, parameters))

    def fetchall(self) -> list[tuple[str]]:
        return [(name,) for name in self._applied]


class _RecordingConnection:
    def __init__(self, statements: list[tuple[str, object]], applied: list[str]) -> None:
        self._cursor = _RecordingCursor(statements, applied)

    def __enter__(self) -> _RecordingConnection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def cursor(self) -> _RecordingCursor:
        return self._cursor

    def transaction(self) -> _RecordingConnection:
        return self


def _run_against_a_recording_database(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, applied: list[str]
) -> list[tuple[str, object]]:
    statements: list[tuple[str, object]] = []
    migration = tmp_path / f"{lowercase_token()}.sql"
    migration.write_text(f"SELECT '{lowercase_token()}'", encoding="utf-8")
    monkeypatch.setattr(RUN_MIGRATIONS, "MIGRATIONS_DIR", tmp_path)
    monkeypatch.setattr(RUN_MIGRATIONS.psycopg, "connect", lambda *_, **__: _RecordingConnection(statements, applied))
    RUN_MIGRATIONS.run_migrations(lowercase_token())
    return statements


def test_a_run_takes_the_migrations_lock_before_it_touches_schema_migrations(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    statements = _run_against_a_recording_database(monkeypatch, tmp_path, [])

    assert statements[0] == (
        RUN_MIGRATIONS.MIGRATIONS_LOCK_SQL,
        (RUN_MIGRATIONS.MIGRATIONS_ADVISORY_LOCK_CLASS, RUN_MIGRATIONS.MIGRATIONS_ADVISORY_LOCK_KEY),
    )
    assert statements[1][0].startswith("CREATE TABLE IF NOT EXISTS schema_migrations")
