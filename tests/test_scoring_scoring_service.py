"""Tests for composite_score()/rank_score(), ported from Tools/PlayStation/test_pipeline.py's
TestCompositeScore/TestRankScore."""

from __future__ import annotations

import pytest

from curator.scoring.scoring_service import composite_score, rank_score


def test_all_three_sources_averaged():
    # PSN 5.0 -> (5-1)/4*100 = 100
    assert composite_score(80, 90, 5.0) == pytest.approx((80 + 90 + 100) / 3, rel=1e-3)


def test_none_values_excluded_from_average():
    assert composite_score(80, None, None) == pytest.approx(80.0)
    assert composite_score(None, 90, None) == pytest.approx(90.0)
    assert composite_score(None, None, 5.0) == pytest.approx(100.0)


def test_all_none_returns_none():
    assert composite_score(None, None, None) is None


def test_psn_normalization_bounds():
    assert composite_score(None, None, 1.0) == pytest.approx(0.0)
    assert composite_score(None, None, 5.0) == pytest.approx(100.0)
    assert composite_score(None, None, 3.0) == pytest.approx(50.0)


def test_composite_85_plus_gives_three_pts():
    assert rank_score(85, None, None) == 3
    assert rank_score(100, None, None) == 3


def test_composite_75_to_84_gives_one_pt():
    assert rank_score(75, None, None) == 1
    assert rank_score(84, None, None) == 1


def test_composite_below_75_gives_zero_pts():
    assert rank_score(74, None, None) == 0
    assert rank_score(0, None, None) == 0


def test_no_composite_gives_zero_pts():
    assert rank_score(None, None, None) == 0


def test_franchise_adds_one_pt():
    assert rank_score(80, None, "God of War") == 2


def test_f2p_subtracts_three_pts():
    assert rank_score(85, "free to play", None) == 0  # 3 - 3
    assert rank_score(85, "live-service", None) == 0
    assert rank_score(85, "free-to-play", None) == 0


def test_f2p_with_high_score_and_franchise():
    assert rank_score(85, "free to play", "Overwatch") == 1  # 3 + 1 - 3


def test_no_multiplayer_not_penalized():
    assert rank_score(85, "", None) == 3
    assert rank_score(85, None, None) == 3
