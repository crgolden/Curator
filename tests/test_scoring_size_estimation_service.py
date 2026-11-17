"""Tests for estimate_install_size_gb(), ported from ps_sizes.py's get_install_size() behavior."""

from __future__ import annotations

from curator.psn.title_platform import PS3, PS4, PS5
from curator.scoring.size_estimation_service import SizeEstimate, estimate_install_size_gb
from test_values import lowercase_token, new_game_title, new_genre_name, new_size_gb

_OVERRIDE_PATTERN = lowercase_token()
_OVERRIDE_PS5_GB = new_size_gb()
_OVERRIDE_PS4_GB = new_size_gb()
_SERIES_PATTERN = lowercase_token()
_SEQUEL_PATTERN = f"{_SERIES_PATTERN} {lowercase_token()}"
_SEQUEL_PS5_GB = new_size_gb()
_SERIES_PS5_GB = new_size_gb()
_TOP_TIER = lowercase_token()
_MIDDLE_TIER = lowercase_token()
_INDIE_TIER = lowercase_token()
_OPEN_WORLD_CLASS = lowercase_token()
_SHOOTER_CLASS = lowercase_token()
_TOP_OPEN_WORLD_PS5_GB = new_size_gb()
_TOP_OPEN_WORLD_PS4_GB = new_size_gb()
_TOP_SHOOTER_PS5_GB = new_size_gb()
_TOP_PS5_GB = new_size_gb()
_TOP_PS4_GB = new_size_gb()
_MIDDLE_PS5_GB = new_size_gb()
_MIDDLE_PS4_GB = new_size_gb()
_INDIE_PS5_GB = new_size_gb()
_INDIE_PS4_GB = new_size_gb()

_ESTIMATES = [
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=_OVERRIDE_PATTERN,
        aaa_tier=None,
        genre_class=None,
        platform=PS5,
        size_gb=_OVERRIDE_PS5_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=_OVERRIDE_PATTERN,
        aaa_tier=None,
        genre_class=None,
        platform=PS4,
        size_gb=_OVERRIDE_PS4_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=_SEQUEL_PATTERN,
        aaa_tier=None,
        genre_class=None,
        platform=PS5,
        size_gb=_SEQUEL_PS5_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=_SERIES_PATTERN,
        aaa_tier=None,
        genre_class=None,
        platform=PS5,
        size_gb=_SERIES_PS5_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=None,
        aaa_tier=_TOP_TIER,
        genre_class=_OPEN_WORLD_CLASS,
        platform=PS5,
        size_gb=_TOP_OPEN_WORLD_PS5_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=None,
        aaa_tier=_TOP_TIER,
        genre_class=_OPEN_WORLD_CLASS,
        platform=PS4,
        size_gb=_TOP_OPEN_WORLD_PS4_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=None,
        aaa_tier=_TOP_TIER,
        genre_class=_SHOOTER_CLASS,
        platform=PS5,
        size_gb=_TOP_SHOOTER_PS5_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=None,
        aaa_tier=_TOP_TIER,
        genre_class=None,
        platform=PS5,
        size_gb=_TOP_PS5_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=None,
        aaa_tier=_TOP_TIER,
        genre_class=None,
        platform=PS4,
        size_gb=_TOP_PS4_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=None,
        aaa_tier=_MIDDLE_TIER,
        genre_class=None,
        platform=PS5,
        size_gb=_MIDDLE_PS5_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=None,
        aaa_tier=_MIDDLE_TIER,
        genre_class=None,
        platform=PS4,
        size_gb=_MIDDLE_PS4_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=None,
        aaa_tier=_INDIE_TIER,
        genre_class=None,
        platform=PS5,
        size_gb=_INDIE_PS5_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=None,
        aaa_tier=_INDIE_TIER,
        genre_class=None,
        platform=PS4,
        size_gb=_INDIE_PS4_GB,
    ),
]


def _open_world_genre():
    return f"{_OPEN_WORLD_CLASS.upper()} {new_genre_name()}"


def test_title_override_wins_over_tier_band():
    result = estimate_install_size_gb(
        _OVERRIDE_PATTERN.upper(), new_genre_name(), platform=PS5, aaa_tier=_TOP_TIER, estimates=_ESTIMATES
    )
    assert result == _OVERRIDE_PS5_GB


def test_title_override_is_platform_specific():
    result = estimate_install_size_gb(
        _OVERRIDE_PATTERN.upper(), new_genre_name(), platform=PS4, aaa_tier=_TOP_TIER, estimates=_ESTIMATES
    )
    assert result == _OVERRIDE_PS4_GB


def test_longest_matching_title_pattern_wins():

    result = estimate_install_size_gb(
        _SEQUEL_PATTERN.upper(), _SHOOTER_CLASS, platform=PS5, aaa_tier=_TOP_TIER, estimates=_ESTIMATES
    )
    assert result == _SEQUEL_PS5_GB


def test_shorter_title_pattern_matches_when_more_specific_one_does_not():
    result = estimate_install_size_gb(
        _SERIES_PATTERN.upper(), _SHOOTER_CLASS, platform=PS5, aaa_tier=_TOP_TIER, estimates=_ESTIMATES
    )
    assert result == _SERIES_PS5_GB


def test_genre_class_band_wins_over_generic_tier_band():
    result = estimate_install_size_gb(
        new_game_title(), _open_world_genre(), platform=PS5, aaa_tier=_TOP_TIER, estimates=_ESTIMATES
    )
    assert result == _TOP_OPEN_WORLD_PS5_GB


def test_generic_tier_band_used_when_no_genre_class_matches():
    result = estimate_install_size_gb(
        new_game_title(), new_genre_name(), platform=PS5, aaa_tier=_TOP_TIER, estimates=_ESTIMATES
    )
    assert result == _TOP_PS5_GB


def test_aa_tier_band():
    result = estimate_install_size_gb(
        new_game_title(), new_genre_name(), platform=PS5, aaa_tier=_MIDDLE_TIER, estimates=_ESTIMATES
    )
    assert result == _MIDDLE_PS5_GB


def test_indie_tier_band():
    result = estimate_install_size_gb(
        new_game_title(), new_genre_name(), platform=PS4, aaa_tier=_INDIE_TIER, estimates=_ESTIMATES
    )
    assert result == _INDIE_PS4_GB


def test_returns_none_when_nothing_matches():
    result = estimate_install_size_gb(
        new_game_title(), new_genre_name(), platform=PS5, aaa_tier=lowercase_token(), estimates=_ESTIMATES
    )
    assert result is None


def test_ps5_and_ps4_use_independent_bands():
    title = new_game_title()
    genre = _open_world_genre()
    ps5 = estimate_install_size_gb(title, genre, platform=PS5, aaa_tier=_TOP_TIER, estimates=_ESTIMATES)
    ps4 = estimate_install_size_gb(title, genre, platform=PS4, aaa_tier=_TOP_TIER, estimates=_ESTIMATES)
    assert ps5 == _TOP_OPEN_WORLD_PS5_GB
    assert ps4 == _TOP_OPEN_WORLD_PS4_GB


def test_empty_estimates_returns_none():
    assert (
        estimate_install_size_gb(new_game_title(), new_genre_name(), platform=PS5, aaa_tier=_TOP_TIER, estimates=[])
        is None
    )


def test_a_platform_with_no_seeded_band_returns_none_rather_than_borrowing_another_platforms():
    assert (
        estimate_install_size_gb(
            new_game_title(), _open_world_genre(), platform=PS3, aaa_tier=_TOP_TIER, estimates=_ESTIMATES
        )
        is None
    ), "0033 seeds PS5 and PS4 only; silently answering with a PS4 band would report a PS3 disc as 64 GB"


_PART_ONE_PATTERN = lowercase_token()
_PART_TWO_PATTERN = f"{_PART_ONE_PATTERN}{lowercase_token()}"
_PART_TWO_REMASTERED_PATTERN = f"{_PART_TWO_PATTERN} {lowercase_token()}"
_PART_ONE_GB = new_size_gb()
_PART_TWO_GB = new_size_gb()
_PART_TWO_REMASTERED_GB = new_size_gb()

_LAST_OF_US_ESTIMATES = [
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=_PART_ONE_PATTERN,
        aaa_tier=None,
        genre_class=None,
        platform=PS5,
        size_gb=_PART_ONE_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=_PART_TWO_REMASTERED_PATTERN,
        aaa_tier=None,
        genre_class=None,
        platform=PS5,
        size_gb=_PART_TWO_REMASTERED_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=_PART_TWO_PATTERN,
        aaa_tier=None,
        genre_class=None,
        platform=PS5,
        size_gb=_PART_TWO_GB,
    ),
]


def test_part_ii_resolves_to_its_own_size_not_the_part_i_prefix_that_also_matches():
    result = estimate_install_size_gb(
        _PART_TWO_PATTERN.upper(), new_genre_name(), platform=PS5, aaa_tier=_TOP_TIER, estimates=_LAST_OF_US_ESTIMATES
    )

    assert result == _PART_TWO_GB


def test_part_ii_remastered_resolves_over_both_shorter_prefixes():
    result = estimate_install_size_gb(
        _PART_TWO_REMASTERED_PATTERN.upper(),
        new_genre_name(),
        platform=PS5,
        aaa_tier=_TOP_TIER,
        estimates=_LAST_OF_US_ESTIMATES,
    )

    assert result == _PART_TWO_REMASTERED_GB
