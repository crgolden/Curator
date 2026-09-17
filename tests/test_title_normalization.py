from __future__ import annotations

import uuid

import pytest

from curator.catalog.title_normalization import normalize_name, normalized_title

TRADEMARK_SYMBOL = "™"
REGISTERED_SYMBOL = "®"
COPYRIGHT_SYMBOL = "©"
UMLAUT_O = "ö"


def _word() -> str:
    return uuid.uuid4().hex


@pytest.mark.parametrize("marker", [TRADEMARK_SYMBOL, REGISTERED_SYMBOL, COPYRIGHT_SYMBOL])
def test_normalize_name_strips_a_trailing_trademark_marker(marker: str) -> None:
    title = _word()

    assert normalize_name(f"{title}{marker}") == title


def test_normalize_name_strips_the_trademark_letters_and_the_parentheses_that_held_them() -> None:
    title = _word()

    assert normalize_name(f"{title} (TM)") == title


def test_normalize_name_keeps_the_letters_tm_inside_a_word() -> None:
    title = f"{_word()}tm{_word()}"

    assert normalize_name(title) == title


def test_normalize_name_drops_accents_the_way_the_ingestion_runtime_does() -> None:
    rest = _word()

    assert normalize_name(f"{UMLAUT_O}{rest}") == f"o{rest}"


def test_normalize_name_collapses_inner_whitespace_and_trims_the_ends() -> None:
    first_word = _word()
    second_word = _word()

    assert normalize_name(f"  {first_word}   {second_word}  ") == f"{first_word} {second_word}"


@pytest.mark.parametrize("name", [None, "", "   ", TRADEMARK_SYMBOL])
def test_normalize_name_answers_none_when_nothing_is_left(name: str | None) -> None:
    assert normalize_name(name) is None


def test_normalized_title_is_the_lower_case_display_form() -> None:
    title = _word().upper()

    assert normalized_title(f" {title} ") == title.lower()
