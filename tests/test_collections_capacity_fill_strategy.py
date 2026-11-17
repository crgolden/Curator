"""Tests for fill_capacity_multi_bin(), generalized from ps_assign_ps5.py/ps_assign_ps4.py's greedy fill
-- and, since WP3, genuinely multi-bin (console-internal + attached storage devices as separate pools)."""

from __future__ import annotations

import pytest

from curator.collections.capacity_fill_strategy import StorageBin, fill_capacity_multi_bin
from curator.collections.game_candidate import GameCandidate
from curator.collections.sort_order import COMPOSITE_DESC
from test_values import (
    lowercase_token,
    new_game_id,
    new_genre_name,
    new_positive_count,
    new_review_score,
    new_small_count,
)


def _candidate(game_id, size_gb, rank_score=0, composite_score=None, genre=""):
    return GameCandidate(
        game_id=game_id,
        title=game_id,
        genre=genre,
        aaa_tier=lowercase_token(),
        franchise="",
        composite_score=composite_score,
        rank_score=rank_score,
        size_gb=size_gb,
    )


def _one_bin(bin_id, capacity_gb):
    return [StorageBin(bin_id=bin_id, capacity_gb=capacity_gb)]


def test_fills_until_capacity_exceeded():
    bin_id = lowercase_token()
    size = new_positive_count()
    lowest_rank = new_small_count()
    middle_rank = lowest_rank + 1
    top_rank = middle_rank + 1
    first, second, third = new_game_id(), new_game_id(), new_game_id()
    candidates = [
        _candidate(first, size, rank_score=top_rank),
        _candidate(second, size, rank_score=middle_rank),
        _candidate(third, size, rank_score=lowest_rank),
    ]

    result = fill_capacity_multi_bin(candidates, _one_bin(bin_id, size + size + size - 1))

    assert [c.game_id for c in result.installed_by_bin[bin_id]] == [first, second]
    assert [c.game_id for c in result.overflow] == [third]
    assert result.used_gb_by_bin[bin_id] == size + size


def test_highest_rank_score_first():
    bin_id = lowercase_token()
    size = new_positive_count()
    low, high = new_game_id(), new_game_id()
    candidates = [_candidate(low, size, rank_score=0), _candidate(high, size, rank_score=new_small_count())]

    result = fill_capacity_multi_bin(candidates, _one_bin(bin_id, size + size))

    assert [c.game_id for c in result.installed_by_bin[bin_id]] == [high, low]


def test_ties_broken_by_composite_score():
    bin_id = lowercase_token()
    size = new_positive_count()
    lower_score = new_review_score()
    first, second = new_game_id(), new_game_id()
    candidates = [
        _candidate(first, size, rank_score=1, composite_score=lower_score),
        _candidate(second, size, rank_score=1, composite_score=lower_score + 1),
    ]

    result = fill_capacity_multi_bin(candidates, _one_bin(bin_id, size + size))

    assert [c.game_id for c in result.installed_by_bin[bin_id]] == [second, first]


def test_none_composite_score_treated_as_zero_for_tiebreak():
    bin_id = lowercase_token()
    size = new_positive_count()
    first, second = new_game_id(), new_game_id()
    candidates = [
        _candidate(first, size, rank_score=1, composite_score=None),
        _candidate(second, size, rank_score=1, composite_score=new_review_score()),
    ]

    result = fill_capacity_multi_bin(candidates, _one_bin(bin_id, size + size))

    assert [c.game_id for c in result.installed_by_bin[bin_id]] == [second, first]


def test_routing_genres_excludes_non_matching_candidates_from_both_lists():
    bin_id = lowercase_token()
    size = new_positive_count()
    routed_genre = new_genre_name()
    routed, unrouted = new_game_id(), new_game_id()
    candidates = [_candidate(routed, size, genre=routed_genre), _candidate(unrouted, size, genre=new_genre_name())]

    result = fill_capacity_multi_bin(candidates, _one_bin(bin_id, size + size), routing_genres=(routed_genre,))

    assert [c.game_id for c in result.installed_by_bin[bin_id]] == [routed]
    assert result.overflow == ()


def test_routing_genres_case_insensitive():
    bin_id = lowercase_token()
    size = new_positive_count()
    routed_genre = new_genre_name()
    routed = new_game_id()
    candidates = [_candidate(routed, size, genre=routed_genre.lower())]

    result = fill_capacity_multi_bin(candidates, _one_bin(bin_id, size), routing_genres=(routed_genre.upper(),))

    assert [c.game_id for c in result.installed_by_bin[bin_id]] == [routed]


def test_no_routing_genres_includes_everything():
    bin_id = lowercase_token()
    size = new_positive_count()
    first, second = new_game_id(), new_game_id()
    candidates = [_candidate(first, size, genre=new_genre_name()), _candidate(second, size, genre=new_genre_name())]

    result = fill_capacity_multi_bin(candidates, _one_bin(bin_id, size + size))

    assert {c.game_id for c in result.installed_by_bin[bin_id]} == {first, second}


def test_empty_candidates_returns_empty_result():
    bin_id = lowercase_token()

    result = fill_capacity_multi_bin([], _one_bin(bin_id, new_positive_count()))

    assert result.installed_by_bin[bin_id] == ()
    assert result.overflow == ()
    assert result.used_gb_by_bin[bin_id] == 0.0


def test_exact_capacity_fit_included():
    bin_id = lowercase_token()
    size = new_positive_count()
    game_id = new_game_id()
    candidates = [_candidate(game_id, size)]

    result = fill_capacity_multi_bin(candidates, _one_bin(bin_id, size))

    assert [c.game_id for c in result.installed_by_bin[bin_id]] == [game_id]


def test_no_bins_sends_everything_to_overflow():
    candidates = [_candidate(new_game_id(), new_positive_count())]

    result = fill_capacity_multi_bin(candidates, [])

    assert result.overflow == (candidates[0],)
    assert result.installed_by_bin == {}


def test_second_bin_absorbs_overflow_from_the_first():
    console_bin_id = lowercase_token()
    device_bin_id = lowercase_token()
    size = new_positive_count()
    first, second = new_game_id(), new_game_id()
    candidates = [
        _candidate(first, size, rank_score=new_small_count() + 1),
        _candidate(second, size, rank_score=1),
    ]
    bins = [StorageBin(console_bin_id, size), StorageBin(device_bin_id, size)]

    result = fill_capacity_multi_bin(candidates, bins)

    assert [c.game_id for c in result.installed_by_bin[console_bin_id]] == [first]
    assert [c.game_id for c in result.installed_by_bin[device_bin_id]] == [second]
    assert result.overflow == ()


def test_sort_order_none_keeps_the_default_rank_score_first_order():
    bin_id = lowercase_token()
    size = new_positive_count()
    lower_score = new_review_score()
    higher_ranked, higher_scored = new_game_id(), new_game_id()
    candidates = [
        _candidate(higher_ranked, size, rank_score=new_small_count() + 1, composite_score=lower_score),
        _candidate(higher_scored, size, rank_score=1, composite_score=lower_score + 1),
    ]

    result = fill_capacity_multi_bin(candidates, _one_bin(bin_id, size + size), sort_order=None)

    assert [c.game_id for c in result.installed_by_bin[bin_id]] == [higher_ranked, higher_scored]


def test_sort_order_composite_desc_ignores_rank_score():
    bin_id = lowercase_token()
    size = new_positive_count()
    lower_score = new_review_score()
    higher_ranked, higher_scored = new_game_id(), new_game_id()
    candidates = [
        _candidate(higher_ranked, size, rank_score=new_small_count() + 1, composite_score=lower_score),
        _candidate(higher_scored, size, rank_score=1, composite_score=lower_score + 1),
    ]

    result = fill_capacity_multi_bin(candidates, _one_bin(bin_id, size + size), sort_order=COMPOSITE_DESC)

    assert [c.game_id for c in result.installed_by_bin[bin_id]] == [higher_scored, higher_ranked]


def test_sort_order_composite_desc_nulls_last():
    bin_id = lowercase_token()
    size = new_positive_count()
    no_score, scored = new_game_id(), new_game_id()
    candidates = [
        _candidate(no_score, size, composite_score=None),
        _candidate(scored, size, composite_score=new_review_score()),
    ]

    result = fill_capacity_multi_bin(candidates, _one_bin(bin_id, size + size), sort_order=COMPOSITE_DESC)

    assert [c.game_id for c in result.installed_by_bin[bin_id]] == [scored, no_score]


def test_unknown_sort_order_raises():
    unknown_sort_order = lowercase_token()
    candidates = [_candidate(new_game_id(), new_positive_count())]
    bins = _one_bin(lowercase_token(), new_positive_count())

    with pytest.raises(ValueError, match=unknown_sort_order):
        fill_capacity_multi_bin(candidates, bins, sort_order=unknown_sort_order)


def test_a_usb_bin_never_appears_when_the_caller_omits_it_for_ps5_candidates():
    bin_id = lowercase_token()
    size = new_positive_count()
    candidates = [_candidate(new_game_id(), size)]
    bins = [StorageBin(bin_id, size - 1)]

    result = fill_capacity_multi_bin(candidates, bins)

    assert result.installed_by_bin[bin_id] == ()
    assert result.overflow == (candidates[0],)
