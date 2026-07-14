"""Ordered franchise-assignment regex matching over ``franchise_rules`` rows.

Ported from ``ps_curate.py``'s ``assign_franchise()``, decoupled from its hardcoded module-level
``FRANCHISE_MAP`` list -- rules now live in the ``franchise_rules`` table and are passed in by the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FranchiseRule:
    """One row from ``franchise_rules``."""

    rule_id: str
    pattern: str
    franchise: str
    priority: int


def assign_franchise(name: str, rules: list[FranchiseRule]) -> str:
    """Return the franchise a title belongs to, or ``""`` if no rule matches.

    Rules are tried in ascending ``priority`` order (lower runs first); the first matching pattern wins --
    mirrors ``ps_curate.py``'s ordered-list-first-match convention, where the list's literal order WAS the
    priority order.

    :param name: The title to classify (need not be pre-normalized -- matching is case-insensitive).
    :param rules: Every franchise rule, in any order (this function sorts by priority itself).
    :returns: The matched franchise name, or ``""``.
    """
    lower = name.lower()
    for rule in sorted(rules, key=lambda r: r.priority):
        if re.search(rule.pattern, lower):
            return rule.franchise
    return ""
