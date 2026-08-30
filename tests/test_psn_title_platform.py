"""Tests for the platform vocabulary and the PSN title-id prefix map."""

from __future__ import annotations

import pytest

from curator.psn.title_platform import (
    CONSOLE_PLATFORM_IDS,
    console_platform,
    is_non_title_entitlement,
    platform_for_title_id,
    platform_vocabulary_message,
)


@pytest.mark.parametrize("platform", CONSOLE_PLATFORM_IDS)
def test_every_platform_in_the_vocabulary_narrows(platform):
    assert console_platform(platform) == platform


def test_a_value_outside_the_vocabulary_raises():
    with pytest.raises(ValueError, match="Xbox"):
        console_platform("Xbox")


def test_narrowing_is_case_sensitive():
    """platforms.platform_id stores the uppercase form and every route contract is written in it, so a
    lowercase value is a caller mistake rather than a spelling to accept silently."""
    with pytest.raises(ValueError, match="ps5"):
        console_platform("ps5")


def test_the_rejection_message_names_every_accepted_platform():
    message = platform_vocabulary_message()

    for platform in CONSOLE_PLATFORM_IDS:
        assert f'"{platform}"' in message, (
            "the message is built from the vocabulary so widening it cannot leave a stale list behind"
        )


@pytest.mark.parametrize(
    ("title_id", "expected"),
    [
        ("PPSA01342_00", "PS5"),
        ("CUSA00207_00", "PS4"),
        ("BLUS30443_00", "PS3"),
        ("BLES00001_00", "PS3"),
        ("NPUB30696_00", "PS3"),
        ("NPUA80145_00", "PS3"),
        ("BCUS98148_00", "PS3"),
        ("NPUO00010_00", "PS3"),
        ("PCSA00134_00", "PSVITA"),
        ("PCSE00972_00", "PSVITA"),
        ("NPVA51210_CN", "PSVITA"),
        ("NPVB03904_CN", "PSVITA"),
        ("UCUS98615_00", "PSP"),
        ("ULUS10041_00", "PSP"),
        ("NPUG80325_00", "PSP"),
    ],
)
def test_a_legacy_entitlement_gets_its_platform_from_the_title_id_prefix(title_id, expected):
    """900 of one account's 3045 entitlements carry no entitlementAttributes at all, so no platformId --
    every one of them is a title whose only per-platform signal is this prefix."""
    assert platform_for_title_id(title_id) == expected


@pytest.mark.parametrize(
    "title_id",
    ["SUBC00002_00", "SCEAPROMO_00", "NPIA90007_01", "NPUP10021_00", "NPUK00035_00"],
)
def test_a_subscription_reward_or_media_sku_is_not_a_title(title_id):
    """NPIA reads like a PS3 prefix and is not one -- it is PS Plus SKUs and their reward children
    ("100% Discount Off", "Exclusive reward"). NPUP/NPUK are Amazon Instant Video, CBS News and
    PlayStation Home themes."""
    assert is_non_title_entitlement(title_id) is True
    assert platform_for_title_id(title_id) is None, (
        "classifying one of these onto a platform would put a discount coupon in a user's PS3 library"
    )


def test_an_unmapped_prefix_reports_no_platform_rather_than_guessing():
    assert platform_for_title_id("ZZZZ00001_00") is None
    assert is_non_title_entitlement("ZZZZ00001_00") is False, (
        "unknown and known-not-a-title are different answers: the first may become a platform later"
    )


@pytest.mark.parametrize("title_id", [None, "", "   "])
def test_a_missing_title_id_reports_no_platform(title_id):
    assert platform_for_title_id(title_id) is None
    assert is_non_title_entitlement(title_id) is False


def test_a_prefix_shorter_than_four_characters_does_not_crash():
    assert platform_for_title_id("CU") is None
