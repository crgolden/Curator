"""Tests for curator.collections.sort_order, the named sort-order allowlist shared by both collection
strategies (capacity_fill_strategy/filter_list_strategy)."""

from __future__ import annotations

import pytest

from curator.collections.game_candidate import GameCandidate
from curator.collections.sort_order import resolve_sort_key


def _candidate(composite_score):
    return GameCandidate(
        game_id="g",
        title="g",
        genre="",
        aaa_tier="AAA",
        franchise="",
        composite_score=composite_score,
        rank_score=0,
        size_gb=0,
    )


def test_none_returns_the_default_key_unchanged():
    default = lambda candidate: (candidate.composite_score,)  # noqa: E731

    key = resolve_sort_key(None, default=default)

    assert key is default


def test_composite_desc_resolves_to_a_different_key_than_the_default():
    default = lambda candidate: ("default-sentinel",)  # noqa: E731

    key = resolve_sort_key("composite_desc", default=default)

    assert key is not default
    assert key(_candidate(42.0)) == (True, 42.0)
    assert key(_candidate(None)) == (False, 0.0)


def test_unknown_name_raises_value_error():
    with pytest.raises(ValueError, match="not_a_real_order"):
        resolve_sort_key("not_a_real_order", default=lambda candidate: (0,))
