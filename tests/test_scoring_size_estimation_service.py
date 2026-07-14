"""Tests for estimate_install_size_gb(), ported from ps_sizes.py's get_install_size() behavior."""

from __future__ import annotations

from curator.scoring.size_estimation_service import SizeEstimate, estimate_install_size_gb

_ESTIMATES = [
    SizeEstimate(
        estimate_id="1", title_pattern="god of war", aaa_tier=None, genre_class=None, platform="PS5", size_gb=50
    ),
    SizeEstimate(
        estimate_id="2", title_pattern="god of war", aaa_tier=None, genre_class=None, platform="PS4", size_gb=45
    ),
    SizeEstimate(
        estimate_id="3",
        title_pattern="call of duty: modern warfare ii",
        aaa_tier=None,
        genre_class=None,
        platform="PS5",
        size_gb=150,
    ),
    SizeEstimate(
        estimate_id="4",
        title_pattern="call of duty: modern warfare",
        aaa_tier=None,
        genre_class=None,
        platform="PS5",
        size_gb=200,
    ),
    SizeEstimate(
        estimate_id="5", title_pattern=None, aaa_tier="AAA", genre_class="open world", platform="PS5", size_gb=81
    ),
    SizeEstimate(
        estimate_id="6", title_pattern=None, aaa_tier="AAA", genre_class="open world", platform="PS4", size_gb=64
    ),
    SizeEstimate(
        estimate_id="7", title_pattern=None, aaa_tier="AAA", genre_class="shooter", platform="PS5", size_gb=68
    ),
    SizeEstimate(estimate_id="8", title_pattern=None, aaa_tier="AAA", genre_class=None, platform="PS5", size_gb=59),
    SizeEstimate(estimate_id="9", title_pattern=None, aaa_tier="AAA", genre_class=None, platform="PS4", size_gb=47),
    SizeEstimate(estimate_id="10", title_pattern=None, aaa_tier="AA", genre_class=None, platform="PS5", size_gb=18),
    SizeEstimate(estimate_id="11", title_pattern=None, aaa_tier="AA", genre_class=None, platform="PS4", size_gb=12),
    SizeEstimate(estimate_id="12", title_pattern=None, aaa_tier="Indie", genre_class=None, platform="PS5", size_gb=16),
    SizeEstimate(estimate_id="13", title_pattern=None, aaa_tier="Indie", genre_class=None, platform="PS4", size_gb=16),
]


def test_title_override_wins_over_tier_band():
    result = estimate_install_size_gb("God of War", "Action", is_ps5=True, aaa_tier="AAA", estimates=_ESTIMATES)
    assert result == 50


def test_title_override_is_platform_specific():
    result = estimate_install_size_gb("God of War", "Action", is_ps5=False, aaa_tier="AAA", estimates=_ESTIMATES)
    assert result == 45


def test_longest_matching_title_pattern_wins():
    # "call of duty: modern warfare ii" (more specific) must win over the shorter
    # "call of duty: modern warfare" substring that also matches.
    result = estimate_install_size_gb(
        "Call of Duty: Modern Warfare II", "Shooter", is_ps5=True, aaa_tier="AAA", estimates=_ESTIMATES
    )
    assert result == 150


def test_shorter_title_pattern_matches_when_more_specific_one_does_not():
    result = estimate_install_size_gb(
        "Call of Duty: Modern Warfare", "Shooter", is_ps5=True, aaa_tier="AAA", estimates=_ESTIMATES
    )
    assert result == 200


def test_genre_class_band_wins_over_generic_tier_band():
    result = estimate_install_size_gb(
        "Some Open World Game", "Open World RPG", is_ps5=True, aaa_tier="AAA", estimates=_ESTIMATES
    )
    assert result == 81


def test_generic_tier_band_used_when_no_genre_class_matches():
    result = estimate_install_size_gb("Some Puzzle Game", "Puzzle", is_ps5=True, aaa_tier="AAA", estimates=_ESTIMATES)
    assert result == 59


def test_aa_tier_band():
    result = estimate_install_size_gb("Some AA Game", "Adventure", is_ps5=True, aaa_tier="AA", estimates=_ESTIMATES)
    assert result == 18


def test_indie_tier_band():
    result = estimate_install_size_gb("Some Indie Game", "Puzzle", is_ps5=False, aaa_tier="Indie", estimates=_ESTIMATES)
    assert result == 16


def test_returns_none_when_nothing_matches():
    result = estimate_install_size_gb(
        "Unknown Game", "Unknown Genre", is_ps5=True, aaa_tier="Unrated", estimates=_ESTIMATES
    )
    assert result is None


def test_ps5_and_ps4_use_independent_bands():
    ps5 = estimate_install_size_gb(
        "Some Open World Game", "Open World", is_ps5=True, aaa_tier="AAA", estimates=_ESTIMATES
    )
    ps4 = estimate_install_size_gb(
        "Some Open World Game", "Open World", is_ps5=False, aaa_tier="AAA", estimates=_ESTIMATES
    )
    assert ps5 == 81
    assert ps4 == 64


def test_empty_estimates_returns_none():
    assert estimate_install_size_gb("Anything", "Action", is_ps5=True, aaa_tier="AAA", estimates=[]) is None
