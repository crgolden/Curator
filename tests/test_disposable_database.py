from __future__ import annotations

from disposable_database import TEST_DATABASE_SUFFIX, TRIAGE_DATABASE_SUFFIX, is_disposable_database
from test_values import lowercase_token


def test_a_database_ending_in_the_triage_suffix_is_disposable() -> None:
    triage_database = lowercase_token() + TRIAGE_DATABASE_SUFFIX

    assert is_disposable_database(triage_database)


def test_a_database_ending_in_the_test_suffix_is_disposable() -> None:
    test_database = lowercase_token() + TEST_DATABASE_SUFFIX

    assert is_disposable_database(test_database)


def test_a_database_with_neither_suffix_is_not_disposable() -> None:
    production_like_database = lowercase_token()

    assert not is_disposable_database(production_like_database)


def test_a_database_carrying_the_triage_suffix_before_its_end_is_not_disposable() -> None:
    triage_mid_name_database = TRIAGE_DATABASE_SUFFIX + lowercase_token()

    assert not is_disposable_database(triage_mid_name_database)


def test_an_absent_database_name_is_not_disposable() -> None:
    assert not is_disposable_database(None)
