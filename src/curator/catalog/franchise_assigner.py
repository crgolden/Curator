"""Ordered franchise-assignment regex matching over ``franchise_rules`` rows.

Ported from ``ps_curate.py``'s ``assign_franchise()``, decoupled from its hardcoded module-level
``FRANCHISE_MAP`` list -- rules now live in the ``franchise_rules`` table and are passed in by the caller.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FranchiseRule:
    """One row from ``franchise_rules``."""

    rule_id: str
    pattern: str
    franchise: str
    priority: int


def fingerprint_franchise_rules(rules: list[FranchiseRule]) -> str:
    """A stable content hash of every franchise rule.

    Used by ``curator.app._enrichment_run_handler`` to skip a full-catalog
    ``CatalogRepository.reclassify_franchise`` pass when ``franchise_rules`` hasn't changed since the last
    time that pass ran -- safe because every newly-canonicalized game is already classified with the
    *current* rules at ingestion time (``canonicalization_service.py``); the reclassification pass only
    exists to retroactively fix pre-existing games after a rule edit.
    """
    canonical = sorted((rule.rule_id, rule.pattern, rule.franchise, rule.priority) for rule in rules)
    return hashlib.sha256(json.dumps(canonical).encode()).hexdigest()


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
