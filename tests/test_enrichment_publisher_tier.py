"""Tests for classify_tier(), ported from ps_enrich.py's classify_tier()/ps_sizes.py's _publisher_tier()."""

from __future__ import annotations

from curator.enrichment.publisher_tier import PublisherTierRule, classify_tier

_RULES = [
    PublisherTierRule(tier_id="1", pattern="sony interactive entertainment", tier="AAA", match_kind="substring"),
    PublisherTierRule(tier_id="2", pattern="electronic arts", tier="AAA", match_kind="substring"),
    PublisherTierRule(tier_id="3", pattern="devolver digital", tier="AA", match_kind="substring"),
    PublisherTierRule(tier_id="4", pattern="team17", tier="AA", match_kind="substring"),
    PublisherTierRule(tier_id="5", pattern="exact publisher name", tier="AAA", match_kind="exact"),
]


def test_aaa_publisher_matches_substring():
    assert classify_tier("Sony Interactive Entertainment LLC", _RULES) == "AAA"


def test_aa_publisher_matches():
    assert classify_tier("Devolver Digital", _RULES) == "AA"


def test_unknown_publisher_defaults_to_indie():
    assert classify_tier("Some Random Indie Studio", _RULES) == "Indie"


def test_empty_publisher_returns_empty_string():
    assert classify_tier("", _RULES) == ""
    assert classify_tier(None, _RULES) == ""


def test_case_insensitive_matching():
    assert classify_tier("ELECTRONIC ARTS", _RULES) == "AAA"


def test_aaa_wins_when_both_aaa_and_aa_would_match():
    rules = [
        PublisherTierRule(tier_id="1", pattern="acme", tier="AA", match_kind="substring"),
        PublisherTierRule(tier_id="2", pattern="acme studios", tier="AAA", match_kind="substring"),
    ]
    assert classify_tier("Acme Studios", rules) == "AAA"


def test_exact_match_kind_requires_full_equality():
    assert classify_tier("Exact Publisher Name", _RULES) == "AAA"
    assert classify_tier("Exact Publisher Name Extra", _RULES) == "Indie"


def test_no_rules_matching_indie_for_nonempty_publisher():
    assert classify_tier("Anything", []) == "Indie"
