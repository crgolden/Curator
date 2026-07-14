"""Tests for fill_capacity(), generalized from ps_assign_ps5.py/ps_assign_ps4.py's greedy bin-pack."""

from __future__ import annotations

from curator.collections.capacity_fill_strategy import fill_capacity
from curator.collections.game_candidate import GameCandidate


def _candidate(game_id, size_gb, rank_score=0, composite_score=None, genre=""):
    return GameCandidate(
        game_id=game_id,
        title=game_id,
        genre=genre,
        aaa_tier="AAA",
        franchise="",
        composite_score=composite_score,
        rank_score=rank_score,
        size_gb=size_gb,
    )


def test_fills_until_capacity_exceeded():
    candidates = [
        _candidate("a", 40, rank_score=3),
        _candidate("b", 40, rank_score=2),
        _candidate("c", 40, rank_score=1),
    ]

    result = fill_capacity(candidates, capacity_gb=90)

    assert [c.game_id for c in result.installed] == ["a", "b"]
    assert [c.game_id for c in result.overflow] == ["c"]
    assert result.used_gb == 80


def test_highest_rank_score_first():
    candidates = [_candidate("low", 10, rank_score=0), _candidate("high", 10, rank_score=3)]

    result = fill_capacity(candidates, capacity_gb=100)

    assert [c.game_id for c in result.installed] == ["high", "low"]


def test_ties_broken_by_composite_score():
    candidates = [
        _candidate("a", 10, rank_score=1, composite_score=60),
        _candidate("b", 10, rank_score=1, composite_score=90),
    ]

    result = fill_capacity(candidates, capacity_gb=100)

    assert [c.game_id for c in result.installed] == ["b", "a"]


def test_none_composite_score_treated_as_zero_for_tiebreak():
    candidates = [
        _candidate("a", 10, rank_score=1, composite_score=None),
        _candidate("b", 10, rank_score=1, composite_score=10),
    ]

    result = fill_capacity(candidates, capacity_gb=100)

    assert [c.game_id for c in result.installed] == ["b", "a"]


def test_routing_genres_excludes_non_matching_candidates_from_both_lists():
    candidates = [_candidate("a", 10, genre="RPG"), _candidate("b", 10, genre="Sports")]

    result = fill_capacity(candidates, capacity_gb=100, routing_genres=("RPG",))

    assert [c.game_id for c in result.installed] == ["a"]
    assert result.overflow == ()


def test_routing_genres_case_insensitive():
    candidates = [_candidate("a", 10, genre="rpg")]

    result = fill_capacity(candidates, capacity_gb=100, routing_genres=("RPG",))

    assert [c.game_id for c in result.installed] == ["a"]


def test_no_routing_genres_includes_everything():
    candidates = [_candidate("a", 10, genre="RPG"), _candidate("b", 10, genre="Sports")]

    result = fill_capacity(candidates, capacity_gb=100)

    assert {c.game_id for c in result.installed} == {"a", "b"}


def test_empty_candidates_returns_empty_result():
    result = fill_capacity([], capacity_gb=100)

    assert result.installed == ()
    assert result.overflow == ()
    assert result.used_gb == 0.0


def test_exact_capacity_fit_included():
    candidates = [_candidate("a", 100)]

    result = fill_capacity(candidates, capacity_gb=100)

    assert [c.game_id for c in result.installed] == ["a"]
