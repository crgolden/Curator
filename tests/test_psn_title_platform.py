import pytest

from curator.psn.title_platform import is_non_title_entitlement, platform_for_title_id


@pytest.mark.parametrize(
    ("title_id", "expected"),
    [
        ("PPSA01527_00", "PS5"),
        ("CUSA00080_00", "PS4"),
        ("BLUS30233_00", "PS3"),
        ("NPIA00001_00", "PS3"),
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


@pytest.mark.parametrize("title_id", ["SUBC00002_00", "SCEAPROMO_00", "NPUP31372_00", "NPUK00035_00"])
def test_entitlements_that_are_not_games_resolve_to_no_platform(title_id: str) -> None:
    assert platform_for_title_id(title_id) is None
    assert is_non_title_entitlement(title_id) is True


@pytest.mark.parametrize("title_id", [None, "", "ZZZZ00001_00"])
def test_absent_or_unrecognised_title_ids_resolve_to_no_platform(title_id: str | None) -> None:
    assert platform_for_title_id(title_id) is None


def test_an_unrecognised_prefix_is_not_reported_as_a_non_title_entitlement() -> None:
    assert is_non_title_entitlement("ZZZZ00001_00") is False


def test_classification_is_case_insensitive() -> None:
    assert platform_for_title_id("blus30233_00") == "PS3"
