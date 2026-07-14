"""Tests for apply_filter_list(), generalized from ps_assign_ps4.py's hardcoded Criterion/Blockbuster
genre-set classification."""

from __future__ import annotations

from curator.collections.collection_spec import CollectionSpec
from curator.collections.filter_list_strategy import apply_filter_list
from curator.collections.game_candidate import GameCandidate


def _candidate(game_id, genre="RPG", aaa_tier="AAA", composite_score=80.0, rank_score=1):
    return GameCandidate(
        game_id=game_id,
        title=game_id,
        genre=genre,
        aaa_tier=aaa_tier,
        franchise="",
        composite_score=composite_score,
        rank_score=rank_score,
        size_gb=10,
    )


def test_no_filters_returns_everything_sorted_by_score():
    candidates = [_candidate("low", composite_score=50), _candidate("high", composite_score=90)]
    spec = CollectionSpec(kind="filter_list")

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == ["high", "low"]


def test_genre_filter_restricts_to_matching_genres():
    candidates = [_candidate("rpg", genre="RPG"), _candidate("sports", genre="Sports")]
    spec = CollectionSpec(kind="filter_list", genre_filter=("RPG",))

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == ["rpg"]


def test_genre_filter_case_insensitive():
    candidates = [_candidate("a", genre="rpg")]
    spec = CollectionSpec(kind="filter_list", genre_filter=("RPG",))

    assert [c.game_id for c in apply_filter_list(candidates, spec)] == ["a"]


def test_min_score_excludes_below_threshold():
    candidates = [_candidate("low", composite_score=70), _candidate("high", composite_score=85)]
    spec = CollectionSpec(kind="filter_list", min_score=80)

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == ["high"]


def test_min_score_excludes_unscored_games():
    candidates = [_candidate("unscored", composite_score=None)]
    spec = CollectionSpec(kind="filter_list", min_score=50)

    assert apply_filter_list(candidates, spec) == []


def test_aaa_tier_filter_restricts_to_matching_tier():
    candidates = [_candidate("aaa", aaa_tier="AAA"), _candidate("indie", aaa_tier="Indie")]
    spec = CollectionSpec(kind="filter_list", aaa_tier_filter="Indie")

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == ["indie"]


def test_combined_filters():
    candidates = [
        _candidate("match", genre="RPG", aaa_tier="AAA", composite_score=90),
        _candidate("wrong_genre", genre="Sports", aaa_tier="AAA", composite_score=90),
        _candidate("wrong_tier", genre="RPG", aaa_tier="Indie", composite_score=90),
        _candidate("too_low", genre="RPG", aaa_tier="AAA", composite_score=10),
    ]
    spec = CollectionSpec(kind="filter_list", genre_filter=("RPG",), min_score=80, aaa_tier_filter="AAA")

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == ["match"]


def test_ties_broken_by_rank_score():
    candidates = [
        _candidate("low_rank", composite_score=80, rank_score=1),
        _candidate("high_rank", composite_score=80, rank_score=3),
    ]
    spec = CollectionSpec(kind="filter_list")

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == ["high_rank", "low_rank"]


def test_no_capacity_limit_returns_all_matching():
    candidates = [_candidate(f"g{i}") for i in range(50)]
    spec = CollectionSpec(kind="filter_list")

    assert len(apply_filter_list(candidates, spec)) == 50
