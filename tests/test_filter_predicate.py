"""Tests for curator.collections.filter_predicate -- evaluate/parse_predicate/predicate_to_dict."""

from __future__ import annotations

import math

import pytest

from curator.collections.filter_predicate import (
    AND_OP,
    GENRE_IN_OP,
    OR_OP,
    PREDICATE_NODES_KEY,
    PREDICATE_OP_KEY,
    PREDICATE_THRESHOLD_KEY,
    PREDICATE_VALUES_KEY,
    SCORE_AT_LEAST_OP,
    TIER_IN_OP,
    And,
    GenreIn,
    Or,
    ScoreAtLeast,
    TierIn,
    evaluate,
    parse_predicate,
    predicate_to_dict,
)
from curator.collections.game_candidate import GameCandidate
from test_values import lowercase_token, new_game_id, new_game_title, new_genre_name, new_review_score, new_size_gb


def _candidate(*, genre, aaa_tier, composite_score=None):
    return GameCandidate(
        game_id=new_game_id(),
        title=new_game_title(),
        genre=genre,
        aaa_tier=aaa_tier,
        franchise="",
        composite_score=composite_score,
        rank_score=1,
        size_gb=new_size_gb(),
    )


def test_genre_in_matches_case_insensitively():
    genre = new_genre_name()
    other_genre = new_genre_name()
    tier = lowercase_token()
    assert evaluate(GenreIn(values=(genre.upper(),)), _candidate(genre=genre.lower(), aaa_tier=tier)) is True
    assert evaluate(GenreIn(values=(genre.upper(),)), _candidate(genre=other_genre, aaa_tier=tier)) is False


def test_tier_in_matches_any_listed_tier():
    genre = new_genre_name()
    listed_tier = lowercase_token()
    other_listed_tier = lowercase_token()
    unlisted_tier = lowercase_token()
    predicate = TierIn(values=(other_listed_tier, listed_tier))
    assert evaluate(predicate, _candidate(genre=genre, aaa_tier=listed_tier)) is True
    assert evaluate(predicate, _candidate(genre=genre, aaa_tier=unlisted_tier)) is False


def test_score_at_least_excludes_unscored_games():
    genre = new_genre_name()
    tier = lowercase_token()
    threshold = new_review_score()
    just_below_threshold = math.nextafter(threshold, 0)
    predicate = ScoreAtLeast(threshold=threshold)
    assert evaluate(predicate, _candidate(genre=genre, aaa_tier=tier, composite_score=None)) is False
    assert evaluate(predicate, _candidate(genre=genre, aaa_tier=tier, composite_score=just_below_threshold)) is False
    assert evaluate(predicate, _candidate(genre=genre, aaa_tier=tier, composite_score=threshold)) is True


def test_and_requires_every_child():
    genre = new_genre_name()
    tier = lowercase_token()
    other_tier = lowercase_token()
    predicate = And(nodes=(GenreIn(values=(genre,)), TierIn(values=(tier,))))
    assert evaluate(predicate, _candidate(genre=genre, aaa_tier=tier)) is True
    assert evaluate(predicate, _candidate(genre=genre, aaa_tier=other_tier)) is False


def test_or_requires_only_one_child():
    genre = new_genre_name()
    other_genre = new_genre_name()
    tier = lowercase_token()
    other_tier = lowercase_token()
    predicate = Or(nodes=(GenreIn(values=(genre,)), TierIn(values=(tier,))))
    assert evaluate(predicate, _candidate(genre=other_genre, aaa_tier=tier)) is True
    assert evaluate(predicate, _candidate(genre=other_genre, aaa_tier=other_tier)) is False


def test_criterion_shape_genre_set_or_action_and_indie():
    """The one concrete requirement this tree exists for: genre in SET or (genre == action and
    tier == indie), from the legacy PS4 "Criterion" classifier."""
    set_genre = new_genre_name()
    other_set_genre = new_genre_name()
    action_genre = new_genre_name()
    unlisted_genre = new_genre_name()
    indie_tier = lowercase_token()
    other_tier = lowercase_token()
    predicate = Or(
        nodes=(
            GenreIn(values=(set_genre, other_set_genre)),
            And(nodes=(GenreIn(values=(action_genre,)), TierIn(values=(indie_tier,)))),
        )
    )

    assert evaluate(predicate, _candidate(genre=set_genre, aaa_tier=other_tier)) is True
    assert evaluate(predicate, _candidate(genre=action_genre, aaa_tier=indie_tier)) is True
    assert evaluate(predicate, _candidate(genre=action_genre, aaa_tier=other_tier)) is False
    assert evaluate(predicate, _candidate(genre=unlisted_genre, aaa_tier=indie_tier)) is False


def test_predicate_to_dict_and_parse_predicate_round_trip():
    predicate = Or(
        nodes=(
            GenreIn(values=(new_genre_name(), new_genre_name())),
            And(
                nodes=(
                    GenreIn(values=(new_genre_name(),)),
                    TierIn(values=(lowercase_token(),)),
                    ScoreAtLeast(threshold=new_review_score()),
                )
            ),
        )
    )

    assert parse_predicate(predicate_to_dict(predicate)) == predicate


_NOT_A_NODE = "an object with an 'op' field"
_UNKNOWN_OP = "Unknown predicate op"
_NOT_A_STRING_LIST = "non-empty list of strings"
_NOT_A_THRESHOLD = "numeric 'threshold'"
_NOT_A_NODE_LIST = "non-empty 'nodes' list"


@pytest.mark.parametrize(
    ("raw", "expected_error"),
    [
        ({}, _NOT_A_NODE),
        ({PREDICATE_OP_KEY: lowercase_token()}, _UNKNOWN_OP),
        ({PREDICATE_OP_KEY: GENRE_IN_OP}, _NOT_A_STRING_LIST),
        ({PREDICATE_OP_KEY: GENRE_IN_OP, PREDICATE_VALUES_KEY: []}, _NOT_A_STRING_LIST),
        ({PREDICATE_OP_KEY: GENRE_IN_OP, PREDICATE_VALUES_KEY: [1, 2]}, _NOT_A_STRING_LIST),
        ({PREDICATE_OP_KEY: TIER_IN_OP, PREDICATE_VALUES_KEY: lowercase_token()}, _NOT_A_STRING_LIST),
        ({PREDICATE_OP_KEY: SCORE_AT_LEAST_OP}, _NOT_A_THRESHOLD),
        ({PREDICATE_OP_KEY: SCORE_AT_LEAST_OP, PREDICATE_THRESHOLD_KEY: lowercase_token()}, _NOT_A_THRESHOLD),
        ({PREDICATE_OP_KEY: SCORE_AT_LEAST_OP, PREDICATE_THRESHOLD_KEY: True}, _NOT_A_THRESHOLD),
        ({PREDICATE_OP_KEY: AND_OP, PREDICATE_NODES_KEY: []}, _NOT_A_NODE_LIST),
        ({PREDICATE_OP_KEY: OR_OP, PREDICATE_NODES_KEY: lowercase_token()}, _NOT_A_NODE_LIST),
        ({PREDICATE_OP_KEY: AND_OP, PREDICATE_NODES_KEY: [{PREDICATE_OP_KEY: lowercase_token()}]}, _UNKNOWN_OP),
        pytest.param(lowercase_token(), _NOT_A_NODE, id="not-a-dict"),
    ],
)
def test_parse_predicate_rejects_malformed_input(raw, expected_error):

    with pytest.raises(ValueError, match=expected_error):
        parse_predicate(raw)
