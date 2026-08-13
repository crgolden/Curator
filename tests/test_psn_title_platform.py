import pytest

from curator.psn.title_platform import is_non_title_entitlement, normalize_platform_id, platform_for_title_id


@pytest.mark.parametrize(
    ("title_id", "expected"),
    [
        ("PPSA01527_00", "PS5"),
        ("CUSA00080_00", "PS4"),
        ("BLUS30233_00", "PS3"),
        ("PCSA00012_00", "PSVITA"),
        ("NPVA25022_CN", "PSVITA"),
        ("UCUS80170_00", "PSP"),
        ("ULUS10063_00", "PSP"),
    ],
)
def test_classifies_every_platform_observed_in_a_real_entitlement_pull(title_id: str, expected: str) -> None:
    assert platform_for_title_id(title_id) == expected


@pytest.mark.parametrize(
    ("title_id", "expected"),
    [
        ("NPUB90126_00", "PS3"),
        ("NPUG80325_00", "PSP"),
        ("NPVB03904_CN", "PSVITA"),
        ("NPUP10021_00", None),
    ],
)
def test_disambiguates_the_shared_np_prefix_by_its_fourth_character(title_id: str, expected: str | None) -> None:
    assert platform_for_title_id(title_id) == expected


@pytest.mark.parametrize(
    "title_id",
    ["SUBC00002_00", "SCEAPROMO_00", "NPUP31372_00", "NPUK00035_00", "NPIA90005_00", "NPIA00001_00"],
)
def test_entitlements_that_are_not_games_resolve_to_no_platform(title_id: str) -> None:
    assert platform_for_title_id(title_id) is None
    assert is_non_title_entitlement(title_id) is True


@pytest.mark.parametrize("title_id", ["NPUB90126_00", "NPUA80231_00", "BLUS30233_00", "NPUO00160_00"])
def test_the_real_ps3_prefixes_are_untouched_by_npia_leaving_the_ps3_table(title_id: str) -> None:
    assert platform_for_title_id(title_id) == "PS3"
    assert is_non_title_entitlement(title_id) is False


@pytest.mark.parametrize("title_id", [None, "", "ZZZZ00001_00"])
def test_absent_or_unrecognised_title_ids_resolve_to_no_platform(title_id: str | None) -> None:
    assert platform_for_title_id(title_id) is None


def test_an_unrecognised_prefix_is_not_reported_as_a_non_title_entitlement() -> None:
    assert is_non_title_entitlement("ZZZZ00001_00") is False


def test_classification_is_case_insensitive() -> None:
    assert platform_for_title_id("blus30233_00") == "PS3"


@pytest.mark.parametrize(
    ("platform_id", "expected"),
    [("ps4", "PS4"), ("ps5", "PS5"), ("ps3", "PS3"), ("psvita", "PSVITA"), ("psp", "PSP"), ("PS4", "PS4")],
)
def test_normalizes_a_recognised_platform_id(platform_id: str, expected: str) -> None:
    assert normalize_platform_id(platform_id) == expected


def test_xperia_is_a_phone_entitlement_not_a_platform() -> None:
    assert normalize_platform_id("xperia") is None


@pytest.mark.parametrize("platform_id", [None, "", "vr2"])
def test_absent_or_unrecognised_platform_ids_normalize_to_none(platform_id: str | None) -> None:
    assert normalize_platform_id(platform_id) is None
