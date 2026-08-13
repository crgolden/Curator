"""Tests for canonicalize()/normalize_name()/edition_rank(), ported from
Tools/PlayStation/test_pipeline.py's TestNormalizeName/TestEditionRank/TestCanonicalize*.

The legacy ``entry()`` dict helper (raw PSN JSON shape) is replaced by building
:class:`~curator.catalog.canonicalization_service.EntitlementSnapshot` directly -- canonicalize()'s real
input shape now that entitlement extraction happens once, at ingestion time, into entitlement_snapshots.
"""

from __future__ import annotations

import pytest

from curator.catalog.canonicalization_service import (
    CanonicalGame,
    EntitlementSnapshot,
    canonicalize,
    edition_rank,
    normalize_name,
)
from curator.catalog.exclusion_rules import ExclusionRule
from curator.catalog.franchise_assigner import FranchiseRule

_EDITION_RANKS = {"director": 1, "complete": 2, "gold": 3, "definitive": 4, "remastered": 5, "ps5": 6}
_NAME_OVERRIDES = {
    "10005732": "Cities: Skylines Remastered",
    "10002926": "Bioshock Infinite: The Complete Edition",
}
_FRANCHISE_RULES = [
    FranchiseRule(rule_id="1", pattern=r"god of war", franchise="God of War", priority=0),
]
_F2P_RULE = ExclusionRule(rule_id="f1", rule_type="f2p_title", pattern="Fortnite")


def _snapshot(
    gm_name,
    pkg="PS4GD",
    tm_name=None,
    concept_id="",
    active=None,
    entitlement_id=None,
    title_id=None,
    platform_ids=(),
    is_game=None,
):
    return EntitlementSnapshot(
        entitlement_id=entitlement_id or f"ent-{gm_name}-{concept_id}-{pkg}",
        concept_id=concept_id or None,
        product_id="UP0000-TEST_00-0000000000000000",
        title_id=title_id,
        game_meta_name=gm_name,
        concept_meta_name=None,
        title_meta_name=tm_name if tm_name is not None else gm_name,
        package_type=pkg,
        active=active,
        platform_ids=platform_ids,
        is_game=is_game,
    )


def _canonicalize(snapshots, *, exclusion_rules=None, franchise_rules=None, name_overrides=None):
    return canonicalize(
        snapshots,
        exclusion_rules=exclusion_rules if exclusion_rules is not None else [],
        franchise_rules=franchise_rules if franchise_rules is not None else _FRANCHISE_RULES,
        edition_ranks=_EDITION_RANKS,
        name_overrides=name_overrides if name_overrides is not None else _NAME_OVERRIDES,
    )


def test_strips_trademark_symbol():
    assert normalize_name("God of War™") == "God of War"


def test_strips_registered_symbol():
    assert normalize_name("PlayStation®") == "PlayStation"


def test_strips_literal_tm_suffix():
    assert normalize_name("ALIENATIONTM") == "ALIENATION"


def test_strips_tm_in_parens():
    assert normalize_name("Trials Rising(TM)") == "Trials Rising"


def test_collapses_whitespace():
    assert normalize_name("God  of   War") == "God of War"


def test_strips_leading_trailing_whitespace():
    assert normalize_name("  Horizon  ") == "Horizon"


def test_directors_cut_beats_complete():
    assert edition_rank("Ghost of Tsushima: Director's Cut", _EDITION_RANKS) < edition_rank(
        "God of War: Complete Edition", _EDITION_RANKS
    )


def test_complete_beats_gold():
    assert edition_rank("Game: Complete Edition", _EDITION_RANKS) < edition_rank("Game: Gold Edition", _EDITION_RANKS)


def test_base_game_ranks_lowest():
    assert edition_rank("God of War", _EDITION_RANKS) > edition_rank("God of War: Gold Edition", _EDITION_RANKS)


def test_remastered_between_definitive_and_ps5():
    r = edition_rank("Horizon Zero Dawn Remastered", _EDITION_RANKS)
    assert edition_rank("Horizon Zero Dawn: Definitive Edition", _EDITION_RANKS) < r
    assert r < edition_rank("Horizon Zero Dawn PS5", _EDITION_RANKS)


def test_active_false_kept_but_flagged_inactive():
    """Inactive entitlements are kept and flagged, not dropped.

    They used to be filtered out here, which meant a PS Plus title the user had lost stayed in their
    library forever: ``library_entries`` is written by an upsert with no delete pass, so once the title
    stopped appearing in the canonicalized set nothing removed or updated the existing row. Carrying the
    flag through to ``library_entries.is_active`` is what actually hides it -- and it makes
    "everything I ever had access to" expressible, which dropping never could.
    """
    data = [_snapshot("Horizon Forbidden West", "PSGD", active=False)]

    games = _canonicalize(data)

    assert len(games) == 1
    assert games[0].active is False


def test_active_missing_treated_as_active():
    data = [_snapshot("God of War", "PS4GD")]

    games = _canonicalize(data)

    assert len(games) == 1
    assert games[0].active is True


def test_active_true_included():
    data = [_snapshot("God of War", "PS4GD", active=True)]

    games = _canonicalize(data)

    assert len(games) == 1
    assert games[0].active is True


def test_concept_with_one_active_entry_is_active():
    data = [
        _snapshot("Game", "PS4GD", concept_id="123", active=False, entitlement_id="e1"),
        _snapshot("Game", "PS4GD", concept_id="123", active=True, entitlement_id="e2"),
    ]

    games = _canonicalize(data)

    assert len(games) == 1
    assert games[0].active is True


def test_concept_with_all_entries_inactive_is_inactive():
    data = [
        _snapshot("Game", "PS4GD", concept_id="456", active=False, entitlement_id="e1"),
        _snapshot("Game", "PSGD", concept_id="456", active=False, entitlement_id="e2"),
    ]

    games = _canonicalize(data)

    assert len(games) == 1
    assert games[0].active is False


def test_an_active_entry_wins_the_edition_tiebreak_over_an_inactive_ps5_one():
    """Active-ness outranks the PSGD-beats-PS4GD rule when picking the winning edition.

    While inactive entries were dropped, the winner was active by construction. Now that they survive,
    an inactive PS5 edition would otherwise beat an active PS4 one on the PSGD term alone -- and
    ``canonical_title``/``product_id``/``winning_entitlement_id`` would name an edition the user cannot
    launch. ``product_id`` in particular feeds the collection cover-art lookup.
    """
    data = [
        _snapshot("Game", "PSGD", concept_id="789", active=False, entitlement_id="inactive-ps5"),
        _snapshot("Game", "PS4GD", concept_id="789", active=True, entitlement_id="active-ps4"),
    ]

    games = _canonicalize(data)

    assert games[0].winning_entitlement_id == "active-ps4"
    assert games[0].native_ps5 is False
    assert games[0].ps4_eligible is True


def test_edition_tiebreak_falls_through_to_an_inactive_entry_when_none_are_active():
    data = [
        _snapshot("Game", "PS4GD", concept_id="999", active=False, entitlement_id="inactive-ps4"),
        _snapshot("Game", "PSGD", concept_id="999", active=False, entitlement_id="inactive-ps5"),
    ]

    games = _canonicalize(data)

    assert games[0].winning_entitlement_id == "inactive-ps5"
    assert games[0].active is False


def test_winning_title_id_follows_the_winning_entry():
    data = [
        _snapshot(
            "Game", "PSGD", concept_id="789", active=False, entitlement_id="inactive-ps5", title_id="PPSA00001_00"
        ),
        _snapshot("Game", "PS4GD", concept_id="789", active=True, entitlement_id="active-ps4", title_id="CUSA00001_00"),
    ]

    games = _canonicalize(data)

    assert games[0].winning_entitlement_id == "active-ps4"
    assert games[0].winning_title_id == "CUSA00001_00"


def test_winning_title_id_is_none_when_the_winning_entry_has_no_title_id():
    data = [_snapshot("Game", "PS4GD", concept_id="1", entitlement_id="e1", title_id=None)]

    games = _canonicalize(data)

    assert games[0].winning_title_id is None


def test_psgd_beats_ps4gd_regardless_of_edition():
    data = [
        _snapshot("Horizon Zero Dawn: Complete Edition", "PS4GD", concept_id="1", entitlement_id="e1"),
        _snapshot("Horizon Zero Dawn Remastered", "PSGD", concept_id="1", entitlement_id="e2"),
    ]
    result = _canonicalize(data)
    assert len(result) == 1
    assert result[0].native_ps5 is True
    assert "Remastered" in result[0].canonical_title


def test_psgd_beats_ps4gd_same_name():
    data = [
        _snapshot("Game Title", "PS4GD", concept_id="2", entitlement_id="e1"),
        _snapshot("Game Title", "PSGD", concept_id="2", entitlement_id="e2"),
    ]
    result = _canonicalize(data)
    assert result[0].native_ps5 is True


def test_directors_cut_beats_base_within_ps4gd():
    data = [
        _snapshot("Ghost of Tsushima", "PS4GD", concept_id="3", entitlement_id="e1"),
        _snapshot("Ghost of Tsushima: Director's Cut", "PS4GD", concept_id="3", entitlement_id="e2"),
    ]
    result = _canonicalize(data)
    assert "Director" in result[0].canonical_title


def test_title_meta_preferred_for_display():
    data = [_snapshot("CoffeeTalk", "PS4GD", tm_name="Coffee Talk")]
    assert _canonicalize(data)[0].canonical_title == "Coffee Talk"


def test_gm_name_used_for_exclusion_not_tm_name():
    exclusion_rules = [ExclusionRule(rule_id="x1", rule_type="name_pattern", pattern=r"bonus content")]
    data = [
        _snapshot("Horizon Zero Dawn Bonus Content App", "PSGD", tm_name="Horizon Zero Dawn"),
    ]
    assert _canonicalize(data, exclusion_rules=exclusion_rules) == []


def test_display_name_concept_override_for_dlc_title_meta():
    concept_id = "10005732"
    assert concept_id in _NAME_OVERRIDES
    data = [
        _snapshot(
            "Cities: Skylines Remastered",
            "PSGD",
            tm_name="Cities: Skylines - Synthetic Dawn Radio",
            concept_id=concept_id,
        )
    ]
    result = _canonicalize(data)
    assert len(result) == 1
    assert result[0].canonical_title == "Cities: Skylines Remastered"


def test_native_ps5_yes_for_psgd():
    data = [_snapshot("Returnal", "PSGD")]
    assert _canonicalize(data)[0].native_ps5 is True


def test_native_ps5_no_for_ps4gd():
    data = [_snapshot("God of War", "PS4GD")]
    assert _canonicalize(data)[0].native_ps5 is False


def test_ps4_eligible_when_ps4gd_exists():
    data = [
        _snapshot("Game", "PS4GD", concept_id="7", entitlement_id="e1"),
        _snapshot("Game", "PSGD", concept_id="7", entitlement_id="e2"),
    ]
    assert _canonicalize(data)[0].ps4_eligible is True


def test_ps4_not_eligible_when_psgd_only():
    data = [_snapshot("Returnal", "PSGD", concept_id="8")]
    assert _canonicalize(data)[0].ps4_eligible is False


def test_a_non_ps4_non_ps5_package_type_is_neither_rather_than_filed_as_ps4():
    data = [_snapshot("Persona 4 Golden", "PSVITAGD", concept_id="9")]

    game = _canonicalize(data)[0]

    assert game.native_ps5 is False
    assert game.ps4_eligible is False


def test_franchise_assigned():
    data = [_snapshot("God of War", "PS4GD")]
    assert _canonicalize(data)[0].franchise == "God of War"


def test_f2p_excluded():
    data = [_snapshot("Fortnite", "PS4GD")]
    assert _canonicalize(data, exclusion_rules=[_F2P_RULE]) == []


def test_deduplication_by_concept_id():
    data = [
        _snapshot("Game", "PS4GD", concept_id="99", entitlement_id="e1"),
        _snapshot("Game", "PS4GD", concept_id="99", entitlement_id="e2"),
    ]
    assert len(_canonicalize(data)) == 1


def test_output_sorted_alphabetically():
    data = [
        _snapshot("Zelda", "PS4GD", concept_id="a"),
        _snapshot("Astro", "PS4GD", concept_id="b"),
    ]
    titles = [g.canonical_title for g in _canonicalize(data)]
    assert titles == sorted(titles, key=str.lower)


def test_globally_excluded_concept_id_is_dropped_even_if_not_matched_by_a_rule():
    data = [_snapshot("Some Game", "PS4GD", concept_id="excluded-1")]

    result = canonicalize(
        data,
        exclusion_rules=[],
        franchise_rules=[],
        edition_ranks={},
        name_overrides={},
        globally_excluded_concept_ids={"excluded-1"},
    )

    assert result == []


def test_canonical_game_carries_every_merged_concept_id():
    data = [
        _snapshot("Game", "PS4GD", concept_id="10", entitlement_id="e1"),
        _snapshot("Game", "PSGD", concept_id="10", entitlement_id="e2"),
    ]
    result = _canonicalize(data)
    assert isinstance(result[0], CanonicalGame)
    assert result[0].concept_ids == ("10",)


def test_an_add_on_marked_is_game_false_is_excluded_even_with_a_game_shaped_package_type():
    """Widening ingestion past the PSGD/PS4GD filter also admits add-on/DLC entitlements PSN itself marks
    ``isGame: false`` (a real example: "Horizon Zero Dawn Artbook", packageType ``PSGD``) -- these must not
    become games just because their packageType and display name look like one.
    """
    data = [_snapshot("Horizon Zero Dawn Artbook", "PSGD", concept_id="addon-1", is_game=False)]

    assert _canonicalize(data) == []


@pytest.mark.parametrize(
    ("name", "package_type"),
    [
        ("Destiny: Rise of Iron - Dynamic Theme", "PS4MISC"),
        ("Harley Quinn Story Pack", "PS4AC"),
        ("Destiny Expansion Pass", "PS4AL"),
        ("Call of Duty: Black Ops Cold War - Campaign 1", "PSAC"),
        ("Destiny 2: The Witch Queen Pre-Order Pack", "PSAL"),
        ("Operation: Tango - Tracker", "PSTRACK"),
        ("Netflix", "PSMEDIA"),
        ("Standard Founders Pack Consumable", "PSCONS"),
        ("PS Plus Premium", "PSSUBS"),
    ],
)
def test_a_non_game_package_type_is_excluded_however_game_shaped_its_name(name: str, package_type: str):
    assert _canonicalize([_snapshot(name, package_type, concept_id=f"c-{package_type}")]) == []


def test_the_package_type_denylist_does_not_touch_the_two_game_package_types():
    data = [
        _snapshot("Worms Rumble", "PSGD", concept_id="ps5-1"),
        _snapshot("Need for Speed Undercover", "PS4GD", concept_id="ps4-1"),
    ]

    assert len(_canonicalize(data)) == 2


def test_is_game_unset_does_not_exclude_a_legacy_entry_that_never_reported_it():
    data = [_snapshot("Shaun White Snowboarding", pkg=None, concept_id="ps3-2", title_id="BLUS30234_00")]

    games = _canonicalize(data)

    assert len(games) == 1
    assert games[0].platforms == ("PS3",)


def test_legacy_entry_with_no_game_meta_name_survives_on_title_meta_name_alone():
    """Some of the legacy entitlements the widened ingestion now admits carry no ``gameMeta.name`` at all
    (per ``LibraryClient``'s own fallback, ``game_meta.get("name") or title_meta.get("name")``) -- the
    drop check must not require ``game_meta_name`` to be present, only that some display name resolves.
    """
    data = [_snapshot(None, pkg=None, tm_name="Shaun White Snowboarding", title_id="BLUS30233_00")]

    games = _canonicalize(data)

    assert len(games) == 1
    assert games[0].canonical_title == "Shaun White Snowboarding"
    assert games[0].platforms == ("PS3",)


def test_a_media_app_with_no_game_meta_name_is_still_excluded_by_its_title_meta_name():
    """The exclusion-name check must fall back to ``title_meta_name`` too, not just the display name --
    otherwise a media-app entitlement whose ``gameMeta.name`` is absent (a real observed shape) bypasses
    the curated ``media_app`` screen entirely once the empty ``gameMeta.name`` is no longer itself a
    drop reason.
    """
    exclusion_rules = [ExclusionRule(rule_id="m1", rule_type="media_app", pattern="Netflix")]
    data = [_snapshot(None, pkg=None, tm_name="Netflix", title_id="CUSA00129_00")]

    assert _canonicalize(data, exclusion_rules=exclusion_rules) == []


def test_legacy_ps3_entry_with_no_package_type_resolves_platform_from_title_id():
    """A legacy entitlement carries no entitlementAttributes and no gameMeta.packageType at all -- the
    platform must come from the title-id prefix instead (AGENTS/PARKING_LOT.md section 7 item 9).
    """
    data = [_snapshot("Shaun White Snowboarding", pkg=None, concept_id="ps3-1", title_id="BLUS30233_00")]

    game = _canonicalize(data)[0]

    assert game.platforms == ("PS3",)
    assert game.native_ps5 is False
    assert game.ps4_eligible is False


def test_legacy_vita_entry_with_no_package_type_resolves_platform_from_title_id():
    data = [_snapshot("Reality Fighters", pkg=None, concept_id="vita-1", title_id="PCSA00012_00")]

    game = _canonicalize(data)[0]

    assert game.platforms == ("PSVITA",)


def test_subscription_entitlement_is_excluded_even_with_a_game_like_display_name():
    """A non-title entitlement must never become a game, regardless of what its names look like -- the
    title-id prefix is the reliable signal, not the (curated, incomplete) exclusion-rules name match.
    """
    data = [_snapshot("PlayStation Plus", pkg=None, concept_id="sub-1", title_id="SUBC00002_00")]

    assert _canonicalize(data) == []


def test_promo_entitlement_is_excluded_by_title_id_prefix():
    data = [_snapshot("Free Trial", pkg=None, concept_id="promo-1", title_id="SCEAPROMO_00")]

    assert _canonicalize(data) == []


@pytest.mark.parametrize(
    ("name", "title_id"),
    [
        ("PS Plus Essential", "NPIA90005_00"),
        ("SP Discount/ Forever; 25% off", "NPIA90005_01"),
        ("PS2 System Data", "NPIA00001_00"),
        ("VSH 400 Beta", "NPIA00061_00"),
    ],
)
def test_npia_entitlement_is_excluded_as_a_non_title_entitlement(name: str, title_id: str):
    """``NPIA`` is Sony's service range -- PS Plus SKUs, their discount/reward child SKUs, system data and
    VSH betas -- not a PS3 game prefix. Real PS3 games arrive under ``NPUB``/``NPUA``/``BLUS``/``NPUO``.
    """
    data = [_snapshot(name, pkg=None, concept_id=f"npia-{title_id}", title_id=title_id)]

    assert _canonicalize(data) == []


def test_ps3_game_prefixes_still_resolve_after_npia_left_the_ps3_prefix_table():
    data = [
        _snapshot("Shaun White Snowboarding", pkg=None, concept_id="ps3-a", title_id="BLUS30233_00"),
        _snapshot("Trine 2", pkg=None, concept_id="ps3-b", title_id="NPUB30567_00"),
        _snapshot("Fat Princess", pkg=None, concept_id="ps3-c", title_id="NPUA80231_00"),
    ]

    assert [game.platforms for game in _canonicalize(data)] == [("PS3",), ("PS3",), ("PS3",)]


def test_no_package_type_is_admitted_when_the_title_has_no_classified_entitlement():
    """The legacy shape: a PS3/Vita/PSP title reports no ``packageType`` on any of its entitlements."""
    data = [
        _snapshot("Retro Game", pkg=None, concept_id="legacy-1", entitlement_id="e1", title_id="NPUB30001_00"),
        _snapshot("Retro Game Deluxe", pkg=None, concept_id="legacy-1", entitlement_id="e2", title_id="NPUB30001_00"),
    ]

    assert len(_canonicalize(data)) == 1


def test_no_package_type_is_admitted_when_the_title_has_a_psgd_sibling():
    data = [
        _snapshot("Modern Game", "PSGD", concept_id="mg-1", entitlement_id="e1", title_id="PPSA00001_00"),
        _snapshot("Modern Game Bonus", pkg=None, concept_id="mg-2", entitlement_id="e2", title_id="PPSA00001_00"),
    ]

    assert len(_canonicalize(data)) == 2


def test_no_package_type_is_admitted_when_the_title_has_a_ps4gd_sibling():
    data = [
        _snapshot("Older Game", "PS4GD", concept_id="og-1", entitlement_id="e1", title_id="CUSA00001_00"),
        _snapshot("Older Game Bonus", pkg=None, concept_id="og-2", entitlement_id="e2", title_id="CUSA00001_00"),
    ]

    assert len(_canonicalize(data)) == 2


def test_no_package_type_is_dropped_when_every_classified_sibling_is_a_non_game():
    """A real example: ``CUSA23827_00`` carries only ``PS4AC``/``PS4AL`` add-ons plus two unclassified
    "Modern Warfare III - Cross-Gen Pack" rows, and no game entitlement at all.
    """
    data = [
        _snapshot("MWIII: Multiplayer", "PS4AC", concept_id="cod-1", entitlement_id="e1", title_id="CUSA23827_00"),
        _snapshot("MWIII Content License", "PS4AL", concept_id="cod-2", entitlement_id="e2", title_id="CUSA23827_00"),
        _snapshot("Call of Duty", pkg=None, concept_id="cod-3", entitlement_id="e3", title_id="CUSA23827_00"),
    ]

    assert _canonicalize(data) == []


def test_no_package_type_is_admitted_when_a_classified_sibling_is_an_unrecognised_package_type():
    data = [
        _snapshot("Odd Sibling", "PS4FUTURE", concept_id="odd-1", entitlement_id="e1", title_id="CUSA00002_00"),
        _snapshot("Unclassified Game", pkg=None, concept_id="odd-2", entitlement_id="e2", title_id="CUSA00002_00"),
    ]

    assert len(_canonicalize(data)) == 2


def test_no_package_type_and_no_title_id_is_admitted_because_it_has_no_siblings_to_consult():
    data = [
        _snapshot("Add-on", "PS4AC", concept_id="nt-1", entitlement_id="e1", title_id="CUSA00003_00"),
        _snapshot("Orphan Game", pkg=None, concept_id="nt-2", entitlement_id="e2", title_id=None),
    ]

    assert [game.canonical_title for game in _canonicalize(data)] == ["Orphan Game"]


def test_the_sibling_rule_is_scoped_to_one_title_id():
    """A denylisted add-on under one title must not suppress an unclassified entitlement under another."""
    data = [
        _snapshot("Some Add-on", "PS4AC", concept_id="s-1", entitlement_id="e1", title_id="CUSA00004_00"),
        _snapshot("Different Game", pkg=None, concept_id="s-2", entitlement_id="e2", title_id="NPUB30002_00"),
    ]

    assert [game.canonical_title for game in _canonicalize(data)] == ["Different Game"]


def test_entry_with_neither_platform_id_nor_a_recognisable_title_id_has_no_platforms():
    data = [_snapshot("Mystery Game", pkg=None, concept_id="mystery-1", title_id="ZZZZ00001_00")]

    game = _canonicalize(data)[0]

    assert game.platforms == ()


def test_entry_with_no_title_id_at_all_has_no_platforms():
    data = [_snapshot("No Title Id Game", pkg=None, concept_id="notitle-1", title_id=None)]

    game = _canonicalize(data)[0]

    assert game.platforms == ()


def test_xperia_platform_id_is_dropped_but_a_co_listed_console_platform_is_kept():
    """``xperia`` marks a phone entitlement co-listed alongside a real console platform, not a platform
    of its own -- it must never end up in ``library_entry_platforms``.
    """
    data = [
        _snapshot("Some PS4 Game", "PS4GD", concept_id="xp-1", title_id="CUSA00080_00", platform_ids=("ps4", "xperia"))
    ]

    game = _canonicalize(data)[0]

    assert game.platforms == ("PS4",)


def test_platform_id_from_attributes_is_preferred_over_the_title_id_fallback():
    data = [_snapshot("Cross-Gen Game", "PSGD", concept_id="cg-1", title_id="CUSA99999_00", platform_ids=("ps5",))]

    game = _canonicalize(data)[0]

    assert game.platforms == ("PS5",)


def test_platforms_union_across_a_merged_cross_buy_concept():
    data = [
        _snapshot(
            "Game", "PS4GD", concept_id="cb-1", entitlement_id="e1", title_id="CUSA00001_00", platform_ids=("ps4",)
        ),
        _snapshot(
            "Game", "PSGD", concept_id="cb-1", entitlement_id="e2", title_id="PPSA00001_00", platform_ids=("ps5",)
        ),
    ]

    game = _canonicalize(data)[0]

    assert game.platforms == ("PS4", "PS5")
