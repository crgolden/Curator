"""Tests for curator.collections.filter_predicate -- evaluate/parse_predicate/predicate_to_dict."""

from __future__ import annotations

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
from test_values import lowercase_token


def _candidate(genre="RPG", aaa_tier="AAA", composite_score=80.0):
    return GameCandidate(
        game_id="g1",
        title="g1",
        genre=genre,
        aaa_tier=aaa_tier,
        franchise="",
        composite_score=composite_score,
        rank_score=1,
        size_gb=10,
    )


def test_genre_in_matches_case_insensitively():
    assert evaluate(GenreIn(values=("RPG",)), _candidate(genre="rpg")) is True
    assert evaluate(GenreIn(values=("RPG",)), _candidate(genre="Sports")) is False


def test_tier_in_matches_any_listed_tier():
    predicate = TierIn(values=("AAA", "AA"))
    assert evaluate(predicate, _candidate(aaa_tier="AA")) is True
    assert evaluate(predicate, _candidate(aaa_tier="Indie")) is False


def test_score_at_least_excludes_unscored_games():
    predicate = ScoreAtLeast(threshold=50.0)
    assert evaluate(predicate, _candidate(composite_score=None)) is False
    assert evaluate(predicate, _candidate(composite_score=49.9)) is False
    assert evaluate(predicate, _candidate(composite_score=50.0)) is True


def test_and_requires_every_child():
    predicate = And(nodes=(GenreIn(values=("RPG",)), TierIn(values=("Indie",))))
    assert evaluate(predicate, _candidate(genre="RPG", aaa_tier="Indie")) is True
    assert evaluate(predicate, _candidate(genre="RPG", aaa_tier="AAA")) is False


def test_or_requires_only_one_child():
    predicate = Or(nodes=(GenreIn(values=("RPG",)), TierIn(values=("Indie",))))
    assert evaluate(predicate, _candidate(genre="Sports", aaa_tier="Indie")) is True
    assert evaluate(predicate, _candidate(genre="Sports", aaa_tier="AAA")) is False


def test_criterion_shape_genre_set_or_action_and_indie():
    """The one concrete requirement this tree exists for: genre in SET or (genre == action and
    tier == indie), from the legacy PS4 "Criterion" classifier."""
    predicate = Or(
        nodes=(
            GenreIn(values=("RPG", "Adventure")),
            And(nodes=(GenreIn(values=("Action",)), TierIn(values=("Indie",)))),
        )
    )

    assert evaluate(predicate, _candidate(genre="RPG", aaa_tier="AAA")) is True
    assert evaluate(predicate, _candidate(genre="Action", aaa_tier="Indie")) is True
    assert evaluate(predicate, _candidate(genre="Action", aaa_tier="AAA")) is False
    assert evaluate(predicate, _candidate(genre="Sports", aaa_tier="Indie")) is False


def test_predicate_to_dict_and_parse_predicate_round_trip():
    predicate = Or(
        nodes=(
            GenreIn(values=("RPG", "Adventure")),
            And(nodes=(GenreIn(values=("Action",)), TierIn(values=("Indie",)), ScoreAtLeast(threshold=60.0))),
        )
    )

    assert parse_predicate(predicate_to_dict(predicate)) == predicate


@pytest.mark.parametrize(
    "raw",
    [
        {},
        {PREDICATE_OP_KEY: lowercase_token()},
        {PREDICATE_OP_KEY: GENRE_IN_OP},
        {PREDICATE_OP_KEY: GENRE_IN_OP, PREDICATE_VALUES_KEY: []},
        {PREDICATE_OP_KEY: GENRE_IN_OP, PREDICATE_VALUES_KEY: [1, 2]},
        {PREDICATE_OP_KEY: TIER_IN_OP, PREDICATE_VALUES_KEY: lowercase_token()},
        {PREDICATE_OP_KEY: SCORE_AT_LEAST_OP},
        {PREDICATE_OP_KEY: SCORE_AT_LEAST_OP, PREDICATE_THRESHOLD_KEY: lowercase_token()},
        {PREDICATE_OP_KEY: SCORE_AT_LEAST_OP, PREDICATE_THRESHOLD_KEY: True},
        {PREDICATE_OP_KEY: AND_OP, PREDICATE_NODES_KEY: []},
        {PREDICATE_OP_KEY: OR_OP, PREDICATE_NODES_KEY: lowercase_token()},
        {PREDICATE_OP_KEY: AND_OP, PREDICATE_NODES_KEY: [{PREDICATE_OP_KEY: lowercase_token()}]},
        pytest.param(lowercase_token(), id="not-a-dict"),
    ],
)
def test_parse_predicate_rejects_malformed_input(raw):

    with pytest.raises(ValueError):  # noqa: PT011
        parse_predicate(raw)
