"""Which database ``db/run_migrations.py`` migrates: the argument the deploy job passes, or the environment
variable the triage gate sets so the password stays off its command line."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

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
