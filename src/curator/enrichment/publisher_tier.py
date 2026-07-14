"""Publisher/developer AAA/AA/Indie tier classification.

Ported from ``ps_enrich.py``'s ``classify_tier()``/``ps_sizes.py``'s ``_publisher_tier()`` -- both scripts
(plus a third implicit copy) carried their own independently-drifted hardcoded ``AAA_PUBLISHERS``/
``AA_PUBLISHERS`` sets. This is the ONE canonical classifier, driven by the ``publisher_tiers`` table
instead of three copies of the same two Python sets.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PublisherTierRule:
    """One row from ``publisher_tiers``."""

    tier_id: str
    pattern: str
    tier: str  # "AAA" | "AA" | "Indie"
    match_kind: str  # "exact" | "substring"


def classify_tier(publisher: str, rules: list[PublisherTierRule]) -> str:
    """Classify a publisher (or developer, as a fallback signal) into AAA/AA/Indie.

    :param publisher: The publisher (or developer) name; case-insensitive.
    :param rules: Every publisher-tier rule, checked in ``"AAA"`` then ``"AA"`` priority order (a name
        matching both an AAA and an AA pattern is classified AAA, mirroring the legacy scripts' set-lookup
        order).
    :returns: ``"AAA"``, ``"AA"``, or ``"Indie"`` (the default when nothing matches) -- or ``""`` if
        ``publisher`` itself is empty (there's nothing to classify).
    """
    lower = (publisher or "").lower()
    if not lower:
        return ""

    def _matches(rule: PublisherTierRule) -> bool:
        pattern = rule.pattern.lower()
        return pattern == lower if rule.match_kind == "exact" else pattern in lower

    if any(_matches(rule) for rule in rules if rule.tier == "AAA"):
        return "AAA"
    if any(_matches(rule) for rule in rules if rule.tier == "AA"):
        return "AA"
    return "Indie"
