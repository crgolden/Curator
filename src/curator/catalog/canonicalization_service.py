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
    sku_id: str | None = None
    active_date: str | None = None
    title_image_url: str | None = None
    game_icon_url: str | None = None
    concept_icon_url: str | None = None
    is_game: bool | None = None
    platform_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GroupedEntry:
    """One entitlement grouped under a concept-id (or name) key, mid-canonicalization.

    ``active`` is carried here rather than recomputed later because
    :func:`~curator.catalog.merge_service.merge_by_product_id_and_name` merges across grouping keys --
    once that has run, a merged group's membership can no longer be traced back to any single key, so the
    original snapshots are unreachable.
    """

    name: str
    package_type: str | None
    concept_id: str | None
    product_id: str | None
    entitlement_id: str
    active: bool | None = None
    #: PSN's store/content title id (e.g. ``"CUSA00419_00"``), carried through purely so a downstream
    #: stage (``curator.library.library_build_orchestrator.LibraryBuildOrchestrator.match_trophies``) can
    #: attempt an exact PS4 trophy-title lookup (``TrophyClient.trophy_titles_for_title``) within the same
    #: build run -- never persisted to ``games``/``library_entries`` itself. See ``CanonicalGame
    #: .winning_title_id``.
    title_id: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalGame:
    """One deduplicated game, ready to persist to ``games``/``game_concepts``.

    ``active`` is the odd one out: every other field describes the game itself and lands in the shared
    catalog, while ``active`` describes *this user's* access and lands on ``library_entries``. See
    ``0012_library_entry_active_state.sql`` for why it must never migrate to ``games``.
    """

    canonical_title: str
    native_ps5: bool
    ps4_eligible: bool
    franchise: str
    product_id: str | None
    concept_ids: tuple[str, ...]
    winning_entitlement_id: str | None
    active: bool = True
    #: The winning edition's PSN store/content title id, if known -- transient, not persisted. See
    #: ``GroupedEntry.title_id``.
    winning_title_id: str | None = None


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
        # Inactive entitlements used to be dropped here. They are kept now and carried through to
        # library_entries.is_active instead: dropping them made "everything I ever had access to"
        # unreachable, and -- because library_entries is upserted with no delete pass -- left a lapsed
        # title stranded in the user's library forever rather than hiding it. See
        # 0012_library_entry_active_state.sql.
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
                # activeFlag missing entirely means active (purchased titles omit it); only an explicit
                # False is an ended entitlement.
                active=snapshot.active,
                title_id=snapshot.title_id,
            )
        )

    merged_groups = merge_by_product_id_and_name(groups)

    canonical: list[CanonicalGame] = []
    for entries in merged_groups.values():
        # Deliberately computed over every entry, active or not: which platforms a game shipped on is a
        # fact about the game, not about whether this user's access to it has lapsed.
        has_ps5 = any(e.package_type == "PSGD" for e in entries)
        has_ps4 = any(e.package_type == "PS4GD" for e in entries)
        # Active entries win outright. This term exists because inactive entitlements used to be filtered
        # out before this line ran, which made the winner active by construction; now that they survive,
        # an inactive PSGD would otherwise beat an active PS4GD and canonical_title/product_id/
        # winning_entitlement_id would start naming an edition the user cannot launch. Ranking active
        # first reproduces the old winner exactly whenever the group has any active entry, and only falls
        # through to an inactive one when every entry is inactive.
        # Below that: PSGD always beats PS4GD regardless of edition rank -- a PS5 native remaster
        # outranks a PS4 Complete Edition in the same concept group. Within the same packageType, prefer
        # the higher-ranked (lower rank number) edition.
        winner = min(
            entries,
            key=lambda e: (
                0 if e.active is not False else 1,
                0 if e.package_type == "PSGD" else 1,
                edition_rank(e.name, edition_ranks),
            ),
        )
        canonical.append(
            CanonicalGame(
                canonical_title=winner.name,
                native_ps5=winner.package_type == "PSGD",
                ps4_eligible=has_ps4 or not has_ps5,
                franchise=assign_franchise(winner.name, franchise_rules),
                product_id=winner.product_id,
                concept_ids=tuple(sorted({e.concept_id for e in entries if e.concept_id})),
                winning_entitlement_id=winner.entitlement_id,
                # Any surviving access keeps the game playable: a user who had a title on PS Plus and
                # later bought it has one inactive and one active entitlement, and still owns the game.
                active=any(e.active is not False for e in entries),
                winning_title_id=winner.title_id,
            )
        )

    return sorted(canonical, key=lambda g: g.canonical_title.lower())
