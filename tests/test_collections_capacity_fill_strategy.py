"""Tests for fill_capacity_multi_bin(), generalized from ps_assign_ps5.py/ps_assign_ps4.py's greedy fill
-- and, since WP3, genuinely multi-bin (console-internal + attached storage devices as separate pools)."""

from __future__ import annotations

from curator.collections.capacity_fill_strategy import StorageBin, fill_capacity_multi_bin
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


def _one_bin(capacity_gb, bin_id="console"):
    return [StorageBin(bin_id=bin_id, capacity_gb=capacity_gb)]


def test_fills_until_capacity_exceeded():
    candidates = [
        _candidate("a", 40, rank_score=3),
        _candidate("b", 40, rank_score=2),
        _candidate("c", 40, rank_score=1),
    ]

    result = fill_capacity_multi_bin(candidates, _one_bin(90))

    assert [c.game_id for c in result.installed_by_bin["console"]] == ["a", "b"]
    assert [c.game_id for c in result.overflow] == ["c"]
    assert result.used_gb_by_bin["console"] == 80


def test_highest_rank_score_first():
    candidates = [_candidate("low", 10, rank_score=0), _candidate("high", 10, rank_score=3)]

    result = fill_capacity_multi_bin(candidates, _one_bin(100))

    assert [c.game_id for c in result.installed_by_bin["console"]] == ["high", "low"]


def test_ties_broken_by_composite_score():
    candidates = [
        _candidate("a", 10, rank_score=1, composite_score=60),
        _candidate("b", 10, rank_score=1, composite_score=90),
    ]

    result = fill_capacity_multi_bin(candidates, _one_bin(100))

    assert [c.game_id for c in result.installed_by_bin["console"]] == ["b", "a"]


def test_none_composite_score_treated_as_zero_for_tiebreak():
    candidates = [
        _candidate("a", 10, rank_score=1, composite_score=None),
        _candidate("b", 10, rank_score=1, composite_score=10),
    ]

    result = fill_capacity_multi_bin(candidates, _one_bin(100))

    assert [c.game_id for c in result.installed_by_bin["console"]] == ["b", "a"]


def test_routing_genres_excludes_non_matching_candidates_from_both_lists():
    candidates = [_candidate("a", 10, genre="RPG"), _candidate("b", 10, genre="Sports")]

    result = fill_capacity_multi_bin(candidates, _one_bin(100), routing_genres=("RPG",))

    assert [c.game_id for c in result.installed_by_bin["console"]] == ["a"]
    assert result.overflow == ()


def test_routing_genres_case_insensitive():
    candidates = [_candidate("a", 10, genre="rpg")]

    result = fill_capacity_multi_bin(candidates, _one_bin(100), routing_genres=("RPG",))

    assert [c.game_id for c in result.installed_by_bin["console"]] == ["a"]


def test_no_routing_genres_includes_everything():
    candidates = [_candidate("a", 10, genre="RPG"), _candidate("b", 10, genre="Sports")]

    result = fill_capacity_multi_bin(candidates, _one_bin(100))

    assert {c.game_id for c in result.installed_by_bin["console"]} == {"a", "b"}


def test_empty_candidates_returns_empty_result():
    result = fill_capacity_multi_bin([], _one_bin(100))

    assert result.installed_by_bin["console"] == ()
    assert result.overflow == ()
    assert result.used_gb_by_bin["console"] == 0.0


def test_exact_capacity_fit_included():
    candidates = [_candidate("a", 100)]

    result = fill_capacity_multi_bin(candidates, _one_bin(100))

    assert [c.game_id for c in result.installed_by_bin["console"]] == ["a"]


def test_no_bins_sends_everything_to_overflow():
    candidates = [_candidate("a", 10)]

    result = fill_capacity_multi_bin(candidates, [])

    assert result.overflow == (candidates[0],)
    assert result.installed_by_bin == {}


def test_second_bin_absorbs_overflow_from_the_first():
    candidates = [_candidate("a", 60, rank_score=2), _candidate("b", 60, rank_score=1)]
    bins = [StorageBin("console", 60.0), StorageBin("device-1", 60.0)]

    result = fill_capacity_multi_bin(candidates, bins)

    assert [c.game_id for c in result.installed_by_bin["console"]] == ["a"]
    assert [c.game_id for c in result.installed_by_bin["device-1"]] == ["b"]
    assert result.overflow == ()


def test_sort_order_none_keeps_the_default_rank_score_first_order():
    candidates = [
        _candidate("a", 10, rank_score=5, composite_score=10),
        _candidate("b", 10, rank_score=1, composite_score=90),
    ]

    result = fill_capacity_multi_bin(candidates, _one_bin(100), sort_order=None)

    assert [c.game_id for c in result.installed_by_bin["console"]] == ["a", "b"]


def test_sort_order_composite_desc_ignores_rank_score():
    candidates = [
        _candidate("a", 10, rank_score=5, composite_score=10),
        _candidate("b", 10, rank_score=1, composite_score=90),
    ]

    result = fill_capacity_multi_bin(candidates, _one_bin(100), sort_order="composite_desc")

    assert [c.game_id for c in result.installed_by_bin["console"]] == ["b", "a"]


def test_sort_order_composite_desc_nulls_last():
    candidates = [
        _candidate("no_score", 10, composite_score=None),
        _candidate("scored", 10, composite_score=1.0),
    ]

    result = fill_capacity_multi_bin(candidates, _one_bin(100), sort_order="composite_desc")

    assert [c.game_id for c in result.installed_by_bin["console"]] == ["scored", "no_score"]


def test_unknown_sort_order_raises():
    try:
        fill_capacity_multi_bin([_candidate("a", 10)], _one_bin(100), sort_order="not_a_real_order")
    except ValueError:
        return
    raise AssertionError("expected a ValueError for an unknown sort_order")


def test_a_usb_bin_never_appears_when_the_caller_omits_it_for_ps5_candidates():
    candidates = [_candidate("a", 500)]
    bins = [StorageBin("console", 100.0)]

    result = fill_capacity_multi_bin(candidates, bins)

    assert result.installed_by_bin["console"] == ()
    assert result.overflow == (candidates[0],)
