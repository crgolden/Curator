"""Game-catalog canonicalization: dedup, PSGD/edition tiebreak, display-name resolution.

Ported from ``Tools\\PlayStation\\ps_curate.py``'s ``canonicalize()``/``normalize_name()``/
``edition_rank()``, decoupled from its Excel-workbook I/O and its hardcoded ``EDITION_RANK``/
``DISPLAY_NAME_BY_CONCEPT`` dicts, which now live in Curator's ``edition_ranks``/``game_name_overrides``
tables and are passed in by the caller (:mod:`curator.catalog.repository`). Pure function, no I/O.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from curator.catalog.exclusion_rules import ExclusionRule, should_exclude
from curator.catalog.franchise_assigner import FranchiseRule, assign_franchise
from curator.catalog.merge_service import merge_by_product_id_and_name


@dataclass(frozen=True, slots=True)
class EntitlementSnapshot:
    """One raw entitlement, as persisted in ``entitlement_snapshots`` -- canonicalization's unit of input."""

    entitlement_id: str
    concept_id: str | None
    product_id: str | None
    title_id: str | None
    game_meta_name: str | None
    concept_meta_name: str | None
    title_meta_name: str | None
    package_type: str | None
    active: bool | None


@dataclass(frozen=True, slots=True)
class GroupedEntry:
    """One entitlement grouped under a concept-id (or name) key, mid-canonicalization."""

    name: str
    package_type: str | None
    concept_id: str | None
    product_id: str | None
    entitlement_id: str


@dataclass(frozen=True, slots=True)
class CanonicalGame:
    """One deduplicated game, ready to persist to ``games``/``game_concepts``."""

    canonical_title: str
    native_ps5: bool
    ps4_eligible: bool
    franchise: str
    product_id: str | None
    concept_ids: tuple[str, ...]
    winning_entitlement_id: str | None


def normalize_name(name: str) -> str:
    """Strip trademark symbols/diacritics and collapse whitespace in a raw PSN title string.

    :param name: The raw title.
    :returns: The normalized title.
    """
    normalized = unicodedata.normalize("NFKD", name)
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    normalized = re.sub(r"[™®©]", "", normalized)
    normalized = re.sub(r"TM\b", "", normalized)  # strip literal "TM" suffix (e.g. "ALIENATIONTM")
    normalized = re.sub(r"\(\s*\)", "", normalized)  # clean up empty parens left by TM removal
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def edition_rank(name: str, ranks: dict[str, int]) -> int:
    """Return the edition-keyword rank for a title (lowest = most preferred edition).

    :param name: The title to rank (need not be pre-normalized -- matching is case-insensitive).
    :param ranks: Keyword -> rank, from the ``edition_ranks`` table.
    :returns: The matched keyword's rank, or ``99`` if no keyword matches.
    """
    lower = name.lower()
    for keyword, rank in sorted(ranks.items(), key=lambda item: item[1]):
        if keyword in lower:
            return rank
    return 99


def canonicalize(
    snapshots: list[EntitlementSnapshot],
    *,
    exclusion_rules: list[ExclusionRule],
    franchise_rules: list[FranchiseRule],
    edition_ranks: dict[str, int],
    name_overrides: dict[str, str],
    globally_excluded_concept_ids: set[str] | None = None,
) -> list[CanonicalGame]:
    """Dedup and canonicalize a set of raw entitlement snapshots into one row per real game.

    :param snapshots: The raw per-entitlement rows to canonicalize.
    :param exclusion_rules: Global exclusion rules (media apps, F2P titles, name patterns, whitelist).
    :param franchise_rules: Franchise-assignment regex rules (see
        :func:`~curator.catalog.franchise_assigner.assign_franchise`).
    :param edition_ranks: Keyword -> rank, used to prefer e.g. "Game of the Year" over "Standard".
    :param name_overrides: ``concept_id`` -> corrected display name, for PSN metadata quirks.
    :param globally_excluded_concept_ids: Concept ids permanently excluded by a past curation decision
        (``global_exclusions``) -- never silently re-included, even if still present in raw entitlements.
    :returns: One :class:`CanonicalGame` per deduplicated game, sorted by title.
    """
    globally_excluded_concept_ids = globally_excluded_concept_ids or set()
    groups: dict[str, list[GroupedEntry]] = {}

    for snapshot in snapshots:
        # activeFlag missing entirely is treated as active (purchased titles omit it); only an explicit
        # False (PS Plus titles that left the catalog or whose subscription lapsed) is skipped.
        if snapshot.active is False:
            continue
        if snapshot.concept_id and snapshot.concept_id in globally_excluded_concept_ids:
            continue

        # Exclusion uses game_meta_name -- it carries "Bonus Content", "Demo", etc. suffixes that
        # title_meta_name often strips.
        gm_name = normalize_name(snapshot.game_meta_name or "")
        if not gm_name or should_exclude(gm_name, exclusion_rules):
            continue

        concept_id = snapshot.concept_id or ""
        # Display name prefers title_meta_name -- it's a per-entitlement field, so it correctly carries
        # edition-specific text (e.g. "DEATH STRANDING DIRECTOR'S CUT") that concept_meta_name does NOT
        # (concept-level, identical across every edition sharing a concept id). name_overrides is the
        # manual escape hatch for the specific concepts where title_meta_name IS wrong (cross-concept
        # metadata corruption).
        raw_display = name_overrides.get(concept_id) or snapshot.title_meta_name or snapshot.game_meta_name or ""
        name = normalize_name(raw_display)
        if not name:
            continue

        key = concept_id or name
        groups.setdefault(key, []).append(
            GroupedEntry(
                name=name,
                package_type=snapshot.package_type,
                concept_id=concept_id or None,
                product_id=snapshot.product_id,
                entitlement_id=snapshot.entitlement_id,
            )
        )

    merged_groups = merge_by_product_id_and_name(groups)

    canonical: list[CanonicalGame] = []
    for entries in merged_groups.values():
        has_ps5 = any(e.package_type == "PSGD" for e in entries)
        has_ps4 = any(e.package_type == "PS4GD" for e in entries)
        # PSGD always beats PS4GD regardless of edition rank -- a PS5 native remaster outranks a PS4
        # Complete Edition in the same concept group. Within the same packageType, prefer the
        # higher-ranked (lower rank number) edition.
        winner = min(entries, key=lambda e: (0 if e.package_type == "PSGD" else 1, edition_rank(e.name, edition_ranks)))
        canonical.append(
            CanonicalGame(
                canonical_title=winner.name,
                native_ps5=winner.package_type == "PSGD",
                ps4_eligible=has_ps4 or not has_ps5,
                franchise=assign_franchise(winner.name, franchise_rules),
                product_id=winner.product_id,
                concept_ids=tuple(sorted({e.concept_id for e in entries if e.concept_id})),
                winning_entitlement_id=winner.entitlement_id,
            )
        )

    return sorted(canonical, key=lambda g: g.canonical_title.lower())
