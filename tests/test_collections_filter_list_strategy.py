"""Tests for apply_filter_list(), generalized from ps_assign_ps4.py's hardcoded Criterion/Blockbuster
genre-set classification."""

from __future__ import annotations

import pytest

from curator.collections.collection_spec import FILTER_LIST_KIND, CollectionSpec
from curator.collections.filter_list_strategy import apply_filter_list, filter_candidates
from curator.collections.filter_predicate import And, GenreIn, Or, TierIn
from curator.collections.game_candidate import GameCandidate
from curator.collections.sort_order import COMPOSITE_DESC
from test_values import (
    lowercase_token,
    new_game_id,
    new_genre_name,
    new_percent_completed,
    new_positive_count,
    new_review_score,
    new_size_gb,
    new_small_count,
)

_DEFAULT_GENRE = new_genre_name()
_DEFAULT_TIER = lowercase_token()
_DEFAULT_SCORE = new_review_score()


def _candidate(
    game_id,
    genre=_DEFAULT_GENRE,
    aaa_tier=_DEFAULT_TIER,
    composite_score=_DEFAULT_SCORE,
    rank_score=1,
    percent_completed=None,
):
    return GameCandidate(
        game_id=game_id,
        title=game_id,
        genre=genre,
        aaa_tier=aaa_tier,
        franchise="",
        composite_score=composite_score,
        rank_score=rank_score,
        size_gb=new_size_gb(),
        percent_completed=percent_completed,
    )


def test_no_filters_returns_everything_sorted_by_score():
    low, high = new_game_id(), new_game_id()
    low_score = new_review_score()
    candidates = [_candidate(low, composite_score=low_score), _candidate(high, composite_score=low_score + 1)]
    spec = CollectionSpec(kind=FILTER_LIST_KIND)

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == [high, low]


def test_genre_filter_restricts_to_matching_genres():
    matching, other = new_game_id(), new_game_id()
    genre = new_genre_name()
    candidates = [_candidate(matching, genre=genre), _candidate(other, genre=new_genre_name())]
    spec = CollectionSpec(kind=FILTER_LIST_KIND, genre_filter=(genre,))

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == [matching]


def test_genre_filter_case_insensitive():
    game_id = new_game_id()
    genre = new_genre_name()
    candidates = [_candidate(game_id, genre=genre.lower())]
    spec = CollectionSpec(kind=FILTER_LIST_KIND, genre_filter=(genre.upper(),))

    assert [c.game_id for c in apply_filter_list(candidates, spec)] == [game_id]


def test_min_score_excludes_below_threshold():
    low, high = new_game_id(), new_game_id()
    min_score = new_review_score()
    candidates = [_candidate(low, composite_score=min_score - 1), _candidate(high, composite_score=min_score + 1)]
    spec = CollectionSpec(kind=FILTER_LIST_KIND, min_score=min_score)

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == [high]


def test_min_score_excludes_unscored_games():
    candidates = [_candidate(new_game_id(), composite_score=None)]
    spec = CollectionSpec(kind=FILTER_LIST_KIND, min_score=new_review_score())

    assert apply_filter_list(candidates, spec) == []


def test_aaa_tier_filter_restricts_to_matching_tier():
    other, matching = new_game_id(), new_game_id()
    tier = lowercase_token()
    candidates = [_candidate(other, aaa_tier=lowercase_token()), _candidate(matching, aaa_tier=tier)]
    spec = CollectionSpec(kind=FILTER_LIST_KIND, aaa_tier_filter=tier)

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == [matching]


def test_combined_filters():
    match, wrong_genre, wrong_tier, too_low = new_game_id(), new_game_id(), new_game_id(), new_game_id()
    genre = new_genre_name()
    tier = lowercase_token()
    min_score = new_review_score()
    passing_score = min_score + 1
    candidates = [
        _candidate(match, genre=genre, aaa_tier=tier, composite_score=passing_score),
        _candidate(wrong_genre, genre=new_genre_name(), aaa_tier=tier, composite_score=passing_score),
        _candidate(wrong_tier, genre=genre, aaa_tier=lowercase_token(), composite_score=passing_score),
        _candidate(too_low, genre=genre, aaa_tier=tier, composite_score=min_score - 1),
    ]
    spec = CollectionSpec(kind=FILTER_LIST_KIND, genre_filter=(genre,), min_score=min_score, aaa_tier_filter=tier)

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == [match]


def test_ties_broken_by_rank_score():
    low_rank, high_rank = new_game_id(), new_game_id()
    score = new_review_score()
    candidates = [
        _candidate(low_rank, composite_score=score, rank_score=1),
        _candidate(high_rank, composite_score=score, rank_score=new_small_count() + 1),
    ]
    spec = CollectionSpec(kind=FILTER_LIST_KIND)

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == [high_rank, low_rank]


def test_no_capacity_limit_returns_all_matching():
    candidate_count = new_positive_count()
    candidates = [_candidate(new_game_id()) for _ in range(candidate_count)]
    spec = CollectionSpec(kind=FILTER_LIST_KIND)

    assert len(apply_filter_list(candidates, spec)) == candidate_count


def test_min_percent_completed_excludes_below_threshold_when_available():
    low, high = new_game_id(), new_game_id()
    min_percent = new_percent_completed()
    candidates = [
        _candidate(low, percent_completed=min_percent - 1),
        _candidate(high, percent_completed=min_percent + 1),
    ]
    spec = CollectionSpec(kind=FILTER_LIST_KIND, min_percent_completed=min_percent)

    result = apply_filter_list(candidates, spec, completion_available=True)

    assert [c.game_id for c in result] == [high]


def test_min_percent_completed_excludes_unmatched_games_when_available():
    candidates = [_candidate(new_game_id(), percent_completed=None)]
    spec = CollectionSpec(kind=FILTER_LIST_KIND, min_percent_completed=1)

    assert apply_filter_list(candidates, spec, completion_available=True) == []


def test_filter_predicate_expresses_the_criterion_or_shape_the_flat_fields_cannot():
    """genre in SET or (genre == action and tier == indie) -- structurally inexpressible with
    genre_filter/aaa_tier_filter's pure-AND flat fields, the reason WP8's predicate tree exists."""
    set_genre_game, action_indie, action_other, unlisted = new_game_id(), new_game_id(), new_game_id(), new_game_id()
    set_genre = new_genre_name()
    action_genre = new_genre_name()
    indie_tier = lowercase_token()
    other_tier = lowercase_token()
    candidates = [
        _candidate(set_genre_game, genre=set_genre, aaa_tier=other_tier),
        _candidate(action_indie, genre=action_genre, aaa_tier=indie_tier),
        _candidate(action_other, genre=action_genre, aaa_tier=other_tier),
        _candidate(unlisted, genre=new_genre_name(), aaa_tier=indie_tier),
    ]
    predicate = Or(
        nodes=(GenreIn(values=(set_genre,)), And(nodes=(GenreIn(values=(action_genre,)), TierIn(values=(indie_tier,)))))
    )
    spec = CollectionSpec(kind=FILTER_LIST_KIND, filter_predicate=predicate)

    result = apply_filter_list(candidates, spec)

    assert {c.game_id for c in result} == {set_genre_game, action_indie}


def test_filter_predicate_replaces_the_flat_fields_rather_than_combining_with_them():
    """When both are supplied, the tree wins outright -- genre_filter/min_score/aaa_tier_filter are simply
    not consulted, per apply_filter_list's docstring."""
    game_id = new_game_id()
    genre = new_genre_name()
    candidates = [_candidate(game_id, genre=genre, aaa_tier=lowercase_token())]
    spec = CollectionSpec(
        kind=FILTER_LIST_KIND,
        genre_filter=(new_genre_name(),),
        filter_predicate=GenreIn(values=(genre,)),
    )

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == [game_id]


def test_filter_predicate_still_respects_the_completion_availability_gate():
    game_id = new_game_id()
    genre = new_genre_name()
    candidates = [_candidate(game_id, genre=genre, percent_completed=None)]
    spec = CollectionSpec(
        kind=FILTER_LIST_KIND, filter_predicate=GenreIn(values=(genre,)), min_percent_completed=new_percent_completed()
    )

    result = apply_filter_list(candidates, spec, completion_available=False)

    assert [c.game_id for c in result] == [game_id]


def test_min_percent_completed_skipped_when_completion_unavailable():
    """A PSN outage / disabled harvest_trophies / broken link must never turn a saved collection empty --
    every candidate legitimately has percent_completed=None in that case, so the predicate is skipped
    entirely rather than excluding everything."""
    first, second = new_game_id(), new_game_id()
    candidates = [_candidate(first, percent_completed=None), _candidate(second, percent_completed=None)]
    spec = CollectionSpec(kind=FILTER_LIST_KIND, min_percent_completed=new_percent_completed())

    result = apply_filter_list(candidates, spec, completion_available=False)

    assert {c.game_id for c in result} == {first, second}


def test_sort_order_none_keeps_the_default_composite_then_rank_order():
    high_composite_low_rank, low_composite_high_rank = new_game_id(), new_game_id()
    low_score = new_review_score()
    candidates = [
        _candidate(high_composite_low_rank, composite_score=low_score + 1, rank_score=1),
        _candidate(low_composite_high_rank, composite_score=low_score, rank_score=new_small_count() + 1),
    ]
    spec = CollectionSpec(kind=FILTER_LIST_KIND, sort_order=None)

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == [high_composite_low_rank, low_composite_high_rank]


def test_sort_order_composite_desc_is_the_same_key_capacity_fill_uses():
    """Confirms filter_list and capacity_fill share one named-sort-order vocabulary
    (curator.collections.sort_order) rather than each strategy inventing its own spelling."""
    no_score, scored = new_game_id(), new_game_id()
    candidates = [_candidate(no_score, composite_score=None), _candidate(scored, composite_score=new_review_score())]
    spec = CollectionSpec(kind=FILTER_LIST_KIND, sort_order=COMPOSITE_DESC)

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == [scored, no_score]


def test_unknown_sort_order_raises():
    unknown_sort_order = lowercase_token()
    spec = CollectionSpec(kind=FILTER_LIST_KIND, sort_order=unknown_sort_order)
    candidates = [_candidate(new_game_id())]

    with pytest.raises(ValueError, match=unknown_sort_order):
        apply_filter_list(candidates, spec)


def test_filter_candidates_is_unsorted_and_shared_by_apply_filter_list():
    """filter_candidates() is the extracted filtering half apply_filter_list() and
    CollectionOrchestrator's capacity_fill pre-filter both build on -- this only proves apply_filter_list
    is filter_candidates() plus a sort, not a second, independently-maintained copy of the same rules."""
    matching, other = new_game_id(), new_game_id()
    genre = new_genre_name()
    candidates = [_candidate(matching, genre=genre), _candidate(other, genre=new_genre_name())]
    spec = CollectionSpec(kind=FILTER_LIST_KIND, genre_filter=(genre,))

    filtered = filter_candidates(candidates, spec)

    assert [c.game_id for c in filtered] == [matching]
    assert [c.game_id for c in apply_filter_list(candidates, spec)] == [c.game_id for c in filtered]
