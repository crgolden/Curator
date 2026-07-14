"""Tests for RAWG fuzzy matching, ported from ps_enrich.py's search_rawg() matching logic."""

from __future__ import annotations

from curator.enrichment.rawg_matcher import PS4_PLATFORM_ID, PS5_PLATFORM_ID, RawgCandidate, find_best_match, similarity


def _candidate(name, platform_ids=frozenset({PS5_PLATFORM_ID}), rawg_game_id=1, released=None):
    return RawgCandidate(rawg_game_id=rawg_game_id, name=name, platform_ids=frozenset(platform_ids), released=released)


def test_exact_title_matches():
    result = find_best_match("God of War", [_candidate("God of War")])
    assert result is not None
    assert result.name == "God of War"


def test_rejects_candidates_without_ps4_or_ps5_platform():
    candidates = [_candidate("God of War", platform_ids=frozenset({4}))]  # PC only
    assert find_best_match("God of War", candidates) is None


def test_accepts_ps4_only_candidate():
    candidates = [_candidate("Bloodborne", platform_ids=frozenset({PS4_PLATFORM_ID}))]
    assert find_best_match("Bloodborne", candidates) is not None


def test_picks_highest_similarity_among_candidates():
    candidates = [
        _candidate("God of War: Ragnarok"),
        _candidate("God of War"),
    ]
    result = find_best_match("God of War", candidates)
    assert result is not None
    assert result.name == "God of War"


def test_below_threshold_returns_none():
    candidates = [_candidate("Something Completely Different")]
    assert find_best_match("God of War", candidates) is None


def test_threshold_is_configurable():
    candidates = [_candidate("God of Wa")]
    assert find_best_match("God of War", candidates, threshold=0.99) is None
    assert find_best_match("God of War", candidates, threshold=0.5) is not None


def test_empty_candidates_returns_none():
    assert find_best_match("Anything", []) is None


def test_similarity_ignores_tm_symbols_and_case():
    assert similarity("God of War™", "GOD OF WAR") == 1.0
