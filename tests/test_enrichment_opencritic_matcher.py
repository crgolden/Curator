"""Tests for the OpenCritic name-matching cascade, ported from ps_opencritic.py's find_match(). One test
per strategy in the cascade, in the order find_match() tries them."""

from __future__ import annotations

from curator.enrichment.opencritic_matcher import (
    OpenCriticGame,
    build_name_index,
    find_match,
    normalize,
    strip_subtitle,
)


def _game(oc_game_id, name, score=80):
    return OpenCriticGame(
        oc_game_id=oc_game_id, name=name, top_critic_score=score, tier="Strong", percent_recommended=90
    )


def test_normalize_converts_roman_numerals():
    assert normalize("Final Fantasy VII") == "final fantasy 7"
    assert normalize("Grand Theft Auto III") == "grand theft auto 3"


def test_normalize_strips_punctuation_and_symbols():
    # NFKD-decomposes "™" to literal "TM" letters before the symbol-strip regex runs (matching
    # ps_opencritic.py's original normalize() exactly) -- so a directly-adjacent "™" merges into the
    # preceding word rather than vanishing cleanly, same as upstream.
    assert normalize("Marvel's Spider-Man 2") == "marvels spider man 2"
    assert normalize("Marvel's Spider-Man™ 2") == "marvels spider mantm 2"


def test_strip_subtitle_on_colon():
    assert strip_subtitle("Sekiro: Shadows Die Twice") == "Sekiro"


def test_strip_subtitle_on_dash():
    assert strip_subtitle("Ghost of Tsushima - Director's Cut") == "Ghost of Tsushima"


def test_strip_subtitle_no_separator_returns_unchanged():
    assert strip_subtitle("Returnal") == "Returnal"


def test_strategy_1_exact_normalized_match():
    games = [_game(1, "Bloodborne")]
    index, nospace_index = build_name_index(games)
    result = find_match("Bloodborne", index, nospace_index)
    assert result is not None
    assert result.oc_game_id == 1


def test_strategy_2_subtitle_stripped_match():
    games = [_game(1, "Sekiro")]
    index, nospace_index = build_name_index(games)
    result = find_match("Sekiro: Shadows Die Twice", index, nospace_index)
    assert result is not None
    assert result.oc_game_id == 1


def test_strategy_3_space_stripped_match():
    # "CoffeeTalk" (workbook) vs. "Coffee Talk" (OpenCritic).
    games = [_game(1, "Coffee Talk")]
    index, nospace_index = build_name_index(games)
    result = find_match("CoffeeTalk", index, nospace_index)
    assert result is not None
    assert result.oc_game_id == 1


def test_strategy_5_substring_fallback_a_our_title_inside_catalog_name():
    # "Skyrim" matching "The Elder Scrolls V: Skyrim - Special Edition".
    games = [_game(1, "The Elder Scrolls V: Skyrim - Special Edition")]
    index, nospace_index = build_name_index(games)
    result = find_match("Skyrim", index, nospace_index)
    assert result is not None
    assert result.oc_game_id == 1


def test_strategy_6_substring_fallback_b_catalog_name_at_start_of_our_title():
    # "Grand Theft Auto III" (OpenCritic) matching "Grand Theft Auto III [en dash] The Definitive
    # Edition" -- strip_subtitle() only recognizes ": "/" - " (plain hyphen), so an en dash separator
    # can't resolve via the earlier subtitle-stripped strategy and must fall through to the substring
    # cascade.
    games = [_game(1, "Grand Theft Auto III")]
    index, nospace_index = build_name_index(games)
    result = find_match("Grand Theft Auto III – The Definitive Edition", index, nospace_index)  # noqa: RUF001
    assert result is not None
    assert result.oc_game_id == 1


def test_strategy_6_prefers_longest_matching_catalog_name():
    games = [_game(1, "Tomb Raider"), _game(2, "Tomb Raider I-III Remastered")]
    index, nospace_index = build_name_index(games)
    result = find_match("Tomb Raider I-III Remastered Starring Lara Croft", index, nospace_index)
    assert result is not None
    assert result.oc_game_id == 2


def test_no_match_returns_none():
    games = [_game(1, "Completely Different Game")]
    index, nospace_index = build_name_index(games)
    assert find_match("God of War", index, nospace_index) is None


def test_best_prefers_highest_scored_candidate_among_duplicates():
    games = [_game(1, "Duplicate Name", score=60), _game(2, "Duplicate Name", score=95)]
    index, nospace_index = build_name_index(games)
    result = find_match("Duplicate Name", index, nospace_index)
    assert result is not None
    assert result.oc_game_id == 2


def test_year_suffix_stripped_name_is_indexed():
    games = [_game(1, "Dead Space (2023)")]
    index, nospace_index = build_name_index(games)
    result = find_match("Dead Space", index, nospace_index)
    assert result is not None
    assert result.oc_game_id == 1
