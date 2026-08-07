"""Tests for canonicalize()/normalize_name()/edition_rank(), ported from
Tools/PlayStation/test_pipeline.py's TestNormalizeName/TestEditionRank/TestCanonicalize*.

The legacy ``entry()`` dict helper (raw PSN JSON shape) is replaced by building
:class:`~curator.catalog.canonicalization_service.EntitlementSnapshot` directly -- canonicalize()'s real
input shape now that entitlement extraction happens once, at ingestion time, into entitlement_snapshots.
"""

from __future__ import annotations

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


def _snapshot(gm_name, pkg="PS4GD", tm_name=None, concept_id="", active=None, entitlement_id=None, title_id=None):
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
