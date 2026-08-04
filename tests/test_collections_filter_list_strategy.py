"""Tests for apply_filter_list(), generalized from ps_assign_ps4.py's hardcoded Criterion/Blockbuster
genre-set classification."""

from __future__ import annotations

from curator.collections.collection_spec import CollectionSpec
from curator.collections.filter_list_strategy import apply_filter_list, filter_candidates
from curator.collections.filter_predicate import And, GenreIn, Or, TierIn
from curator.collections.game_candidate import GameCandidate


def _candidate(game_id, genre="RPG", aaa_tier="AAA", composite_score=80.0, rank_score=1, percent_completed=None):
    return GameCandidate(
        game_id=game_id,
        title=game_id,
        genre=genre,
        aaa_tier=aaa_tier,
        franchise="",
        composite_score=composite_score,
        rank_score=rank_score,
        size_gb=10,
        percent_completed=percent_completed,
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


def test_min_percent_completed_excludes_below_threshold_when_available():
    candidates = [_candidate("low", percent_completed=20), _candidate("high", percent_completed=80)]
    spec = CollectionSpec(kind="filter_list", min_percent_completed=50)

    result = apply_filter_list(candidates, spec, completion_available=True)

    assert [c.game_id for c in result] == ["high"]


def test_min_percent_completed_excludes_unmatched_games_when_available():
    candidates = [_candidate("unmatched", percent_completed=None)]
    spec = CollectionSpec(kind="filter_list", min_percent_completed=1)

    assert apply_filter_list(candidates, spec, completion_available=True) == []


def test_filter_predicate_expresses_the_criterion_or_shape_the_flat_fields_cannot():
    """genre in SET or (genre == action and tier == indie) -- structurally inexpressible with
    genre_filter/aaa_tier_filter's pure-AND flat fields, the reason WP8's predicate tree exists."""
    candidates = [
        _candidate("rpg", genre="RPG", aaa_tier="AAA"),
        _candidate("action_indie", genre="Action", aaa_tier="Indie"),
        _candidate("action_aaa", genre="Action", aaa_tier="AAA"),
        _candidate("sports", genre="Sports", aaa_tier="Indie"),
    ]
    predicate = Or(
        nodes=(GenreIn(values=("RPG",)), And(nodes=(GenreIn(values=("Action",)), TierIn(values=("Indie",)))))
    )
    spec = CollectionSpec(kind="filter_list", filter_predicate=predicate)

    result = apply_filter_list(candidates, spec)

    assert {c.game_id for c in result} == {"rpg", "action_indie"}


def test_filter_predicate_replaces_the_flat_fields_rather_than_combining_with_them():
    """When both are supplied, the tree wins outright -- genre_filter/min_score/aaa_tier_filter are simply
    not consulted, per apply_filter_list's docstring."""
    candidates = [_candidate("a", genre="Sports", aaa_tier="Indie")]
    spec = CollectionSpec(
        kind="filter_list",
        genre_filter=("RPG",),  # would exclude "a" under the flat path
        filter_predicate=GenreIn(values=("Sports",)),
    )

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == ["a"]


def test_filter_predicate_still_respects_the_completion_availability_gate():
    candidates = [_candidate("a", genre="RPG", percent_completed=None)]
    spec = CollectionSpec(kind="filter_list", filter_predicate=GenreIn(values=("RPG",)), min_percent_completed=50)

    result = apply_filter_list(candidates, spec, completion_available=False)

    assert [c.game_id for c in result] == ["a"]


def test_min_percent_completed_skipped_when_completion_unavailable():
    """A PSN outage / disabled harvest_trophies / broken link must never turn a saved collection empty --
    every candidate legitimately has percent_completed=None in that case, so the predicate is skipped
    entirely rather than excluding everything."""
    candidates = [_candidate("a", percent_completed=None), _candidate("b", percent_completed=None)]
    spec = CollectionSpec(kind="filter_list", min_percent_completed=50)

    result = apply_filter_list(candidates, spec, completion_available=False)

    assert {c.game_id for c in result} == {"a", "b"}


def test_sort_order_none_keeps_the_default_composite_then_rank_order():
    candidates = [
        _candidate("high_composite_low_rank", composite_score=90, rank_score=1),
        _candidate("low_composite_high_rank", composite_score=10, rank_score=5),
    ]
    spec = CollectionSpec(kind="filter_list", sort_order=None)

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == ["high_composite_low_rank", "low_composite_high_rank"]


def test_sort_order_composite_desc_is_the_same_key_capacity_fill_uses():
    """Confirms filter_list and capacity_fill share one named-sort-order vocabulary
    (curator.collections.sort_order) rather than each strategy inventing its own spelling."""
    candidates = [_candidate("no_score", composite_score=None), _candidate("scored", composite_score=1.0)]
    spec = CollectionSpec(kind="filter_list", sort_order="composite_desc")

    result = apply_filter_list(candidates, spec)

    assert [c.game_id for c in result] == ["scored", "no_score"]


def test_unknown_sort_order_raises():
    spec = CollectionSpec(kind="filter_list", sort_order="not_a_real_order")

    try:
        apply_filter_list([_candidate("a")], spec)
    except ValueError:
        return
    raise AssertionError("expected a ValueError for an unknown sort_order")


def test_filter_candidates_is_unsorted_and_shared_by_apply_filter_list():
    """filter_candidates() is the extracted filtering half apply_filter_list() and
    CollectionOrchestrator's capacity_fill pre-filter both build on -- this only proves apply_filter_list
    is filter_candidates() plus a sort, not a second, independently-maintained copy of the same rules."""
    candidates = [_candidate("rpg", genre="RPG"), _candidate("sports", genre="Sports")]
    spec = CollectionSpec(kind="filter_list", genre_filter=("RPG",))

    filtered = filter_candidates(candidates, spec)

    assert [c.game_id for c in filtered] == ["rpg"]
    assert [c.game_id for c in apply_filter_list(candidates, spec)] == [c.game_id for c in filtered]
