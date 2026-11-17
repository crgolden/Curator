"""Tests for curator.collections.sort_order, the named sort-order allowlist shared by both collection
strategies (capacity_fill_strategy/filter_list_strategy)."""

from __future__ import annotations

import pytest

from curator.collections.game_candidate import GameCandidate
from curator.collections.sort_order import COMPOSITE_DESC, resolve_sort_key
from test_values import lowercase_token, new_game_id, new_game_title, new_genre_name, new_review_score


def _candidate(composite_score):
    return GameCandidate(
        game_id=new_game_id(),
        title=new_game_title(),
        genre=new_genre_name(),
        aaa_tier=lowercase_token(),
        franchise=lowercase_token(),
        composite_score=composite_score,
        rank_score=0,
        size_gb=0,
    )


def test_none_returns_the_default_key_unchanged():
    def default(candidate):
        return (candidate.composite_score,)

    key = resolve_sort_key(None, default=default)

    assert key is default


def test_composite_desc_resolves_to_a_different_key_than_the_default():
    sentinel = lowercase_token()
    score = new_review_score()

    def default(candidate):
        return (sentinel,)

    key = resolve_sort_key(COMPOSITE_DESC, default=default)

    assert key is not default
    assert key(_candidate(score)) == (True, score)
    assert key(_candidate(None)) == (False, 0.0)


def test_unknown_name_raises_value_error():
    unknown_order = lowercase_token()

    def default(candidate):
        return (0,)

    with pytest.raises(ValueError, match=unknown_order):
        resolve_sort_key(unknown_order, default=default)
