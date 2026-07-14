"""Tests for should_exclude(), ported from Tools/PlayStation/test_pipeline.py's TestShouldExclude.

The legacy hardcoded MEDIA_APPS/F2P_TITLES/NAME_EXCLUDE_PATTERNS/EXCLUSION_WHITELIST module-level
constants are represented here as ExclusionRule rows (their real replacement, the exclusion_rules table).
"""

from __future__ import annotations

from curator.catalog.exclusion_rules import ExclusionRule, should_exclude

_RULES = [
    ExclusionRule(rule_id="1", rule_type="media_app", pattern="Netflix"),
    ExclusionRule(rule_id="2", rule_type="media_app", pattern="VUDU"),
    ExclusionRule(rule_id="3", rule_type="f2p_title", pattern="Fortnite"),
    ExclusionRule(rule_id="4", rule_type="f2p_title", pattern="Destiny 2"),
    ExclusionRule(rule_id="5", rule_type="name_pattern", pattern=r"\bdemo\b"),
    ExclusionRule(rule_id="6", rule_type="name_pattern", pattern=r"\bsoundtrack\b"),
    ExclusionRule(rule_id="7", rule_type="name_pattern", pattern=r"\bbeta\b"),
    ExclusionRule(rule_id="8", rule_type="name_pattern", pattern=r"\bdlc\b"),
    ExclusionRule(rule_id="9", rule_type="name_pattern", pattern=r"\bseason pass\b"),
    ExclusionRule(rule_id="10", rule_type="whitelist", pattern="Ghost of Tsushima - Bonus Content"),
]


def test_excludes_media_app_exact():
    assert should_exclude("Netflix", _RULES) is True


def test_excludes_media_app_with_suffix():
    # "VUDU HD Movies" must match "VUDU" via startswith.
    assert should_exclude("VUDU HD Movies", _RULES) is True


def test_excludes_f2p_title():
    assert should_exclude("Fortnite", _RULES) is True


def test_excludes_destiny_2():
    assert should_exclude("Destiny 2", _RULES) is True


def test_excludes_demo():
    assert should_exclude("Horizon Zero Dawn Demo", _RULES) is True


def test_excludes_soundtrack():
    assert should_exclude("Final Fantasy VII Soundtrack", _RULES) is True


def test_excludes_beta():
    assert should_exclude("Destiny 2 Beta", _RULES) is True


def test_excludes_dlc_keyword():
    assert should_exclude("God of War DLC Pack", _RULES) is True


def test_excludes_season_pass():
    assert should_exclude("Assassin's Creed Season Pass", _RULES) is True


def test_whitelist_overrides_exclusion():
    # Ghost of Tsushima - Bonus Content is a full standalone mode, not DLC.
    assert should_exclude("Ghost of Tsushima - Bonus Content", _RULES) is False


def test_normal_game_not_excluded():
    assert should_exclude("God of War", _RULES) is False


def test_call_of_duty_infinite_warfare_not_excluded():
    # Paid title; only Warzone (F2P) is excluded from the Call of Duty family.
    assert should_exclude("Call of Duty: Infinite Warfare", _RULES) is False


def test_whitelist_wins_even_when_a_name_pattern_would_also_match():
    rules = [
        *_RULES,
        ExclusionRule(rule_id="11", rule_type="name_pattern", pattern=r"bonus content"),
    ]
    assert should_exclude("Ghost of Tsushima - Bonus Content", rules) is False


def test_no_rules_never_excludes():
    assert should_exclude("Anything", []) is False
