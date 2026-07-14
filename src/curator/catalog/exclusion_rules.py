"""Pure exclusion predicates over ``exclusion_rules`` rows.

Ported from ``ps_curate.py``'s ``should_exclude()``, decoupled from its four hardcoded module-level
sets/lists (``MEDIA_APPS``/``F2P_TITLES``/``NAME_EXCLUDE_PATTERNS``/``EXCLUSION_WHITELIST``), which now
live in the ``exclusion_rules`` table and are passed in as rows by the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExclusionRule:
    """One row from ``exclusion_rules``."""

    rule_id: str
    rule_type: str  # "media_app" | "f2p_title" | "name_pattern" | "whitelist"
    pattern: str


def should_exclude(name: str, rules: list[ExclusionRule]) -> bool:
    """Decide whether a (normalized) title should be dropped from curation entirely.

    A whitelist match always wins, regardless of any other matching rule -- mirrors ``ps_curate.py``'s
    ``EXCLUSION_WHITELIST`` short-circuit (e.g. a title whose name happens to match an F2P pattern but is
    explicitly known-good).

    :param name: The already-normalized title (see
        :func:`~curator.catalog.canonicalization_service.normalize_name`).
    :param rules: Every active exclusion rule.
    :returns: ``True`` if the title should be excluded.
    """
    whitelisted = {rule.pattern for rule in rules if rule.rule_type == "whitelist"}
    if name in whitelisted:
        return False

    f2p_titles = {rule.pattern for rule in rules if rule.rule_type == "f2p_title"}
    if name in f2p_titles:
        return True

    media_apps = [rule.pattern for rule in rules if rule.rule_type == "media_app"]
    if any(name == app or name.startswith(f"{app} ") or name.startswith(f"{app}:") for app in media_apps):
        return True

    lower = name.lower()
    name_patterns = [rule.pattern for rule in rules if rule.rule_type == "name_pattern"]
    return any(re.search(pattern, lower) for pattern in name_patterns)
