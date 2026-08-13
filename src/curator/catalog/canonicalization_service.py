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
from curator.psn.title_platform import is_non_title_entitlement, normalize_platform_id, platform_for_title_id

_NON_GAME_PACKAGE_TYPES: frozenset[str] = frozenset(
    {"PS4MISC", "PS4AC", "PS4AL", "PSAC", "PSAL", "PSTRACK", "PSMEDIA", "PSCONS", "PSSUBS"}
)


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
    title_id: str | None = None
    platforms: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class CanonicalGame:
    """One deduplicated game, ready to persist to ``games``/``game_concepts``.

    ``active`` is the odd one out: every other field describes the game itself and lands in the shared
    catalog, while ``active`` describes *this user's* access and lands on ``library_entries``. See
    ``0012_library_entry_active_state.sql`` for why it must never migrate to ``games``.

    ``platforms`` is the union of every merged entry's resolved platform (``platforms.platform_id``
    values), independent of ``native_ps5``/``ps4_eligible`` -- it is what feeds
    ``library_entry_platforms``, and covers platforms those two booleans cannot express (PS3, PS Vita,
    PSP). Empty when no entry resolved a platform at all.
    """

    canonical_title: str
    native_ps5: bool
    ps4_eligible: bool
    franchise: str
    product_id: str | None
    concept_ids: tuple[str, ...]
    winning_entitlement_id: str | None
    active: bool = True
    winning_title_id: str | None = None
    platforms: tuple[str, ...] = ()


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


def _titles_classified_entirely_as_non_game(snapshots: list[EntitlementSnapshot]) -> frozenset[str]:
    """Return the title ids whose every package-typed entitlement carries a non-game package type.

    A title with no package-typed entitlement at all does not appear, nor does one carrying any package
    type outside :data:`_NON_GAME_PACKAGE_TYPES`.

    :param snapshots: The raw per-entitlement rows to index.
    """
    classified_types: dict[str, set[str]] = {}
    for snapshot in snapshots:
        if snapshot.title_id and snapshot.package_type is not None:
            classified_types.setdefault(snapshot.title_id, set()).add(snapshot.package_type)
    return frozenset(title_id for title_id, types in classified_types.items() if types <= _NON_GAME_PACKAGE_TYPES)


def _resolve_platforms(snapshot: EntitlementSnapshot) -> frozenset[str]:
    """Resolve one entitlement's platform set, preferring PSN's own attributes over the title id.

    :param snapshot: The raw entitlement to classify.
    :returns: The recognised platforms from ``entitlementAttributes`` (normalized, non-console values
        such as ``"xperia"`` dropped); when that yields nothing, the single platform
        :func:`~curator.psn.title_platform.platform_for_title_id` derives from ``title_id``; empty when
        neither signal resolves a platform.
    """
    from_attributes = frozenset(
        platform for platform in (normalize_platform_id(raw) for raw in snapshot.platform_ids) if platform
    )
    if from_attributes:
        return from_attributes

    from_title_id = platform_for_title_id(snapshot.title_id)
    return frozenset({from_title_id}) if from_title_id else frozenset()


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

    A snapshot is dropped from the result when its concept id is in ``globally_excluded_concept_ids``,
    when :func:`~curator.psn.title_platform.is_non_title_entitlement` reports its title id as a
    non-title entitlement, when ``is_game`` is ``False``, when its ``package_type`` names add-on,
    theme/avatar, tracker, media-app, consumable or subscription content, or when its resolved name
    matches ``exclusion_rules``. A ``package_type`` of ``None`` is a reason to drop only when every other
    entitlement sharing that ``title_id`` which PSN *did* classify carries a non-game package type; legacy
    PS3, Vita and PSP entitlements report no package type on any of their rows, so they are unaffected.

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
    non_game_titles = _titles_classified_entirely_as_non_game(snapshots)
    groups: dict[str, list[GroupedEntry]] = {}

    for snapshot in snapshots:
        if snapshot.concept_id and snapshot.concept_id in globally_excluded_concept_ids:
            continue

        if is_non_title_entitlement(snapshot.title_id):
            continue

        if snapshot.is_game is False:
            continue

        if (snapshot.package_type or "") in _NON_GAME_PACKAGE_TYPES:
            continue

        if snapshot.package_type is None and snapshot.title_id in non_game_titles:
            continue

        gm_name = normalize_name(snapshot.game_meta_name or "")
        exclusion_name = gm_name or normalize_name(snapshot.title_meta_name or "")
        if should_exclude(exclusion_name, exclusion_rules):
            continue

        concept_id = snapshot.concept_id or ""
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
                active=snapshot.active,
                title_id=snapshot.title_id,
                platforms=_resolve_platforms(snapshot),
            )
        )

    merged_groups = merge_by_product_id_and_name(groups)

    canonical: list[CanonicalGame] = []
    for entries in merged_groups.values():
        has_ps4 = any(e.package_type == "PS4GD" for e in entries)
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
                ps4_eligible=has_ps4,
                franchise=assign_franchise(winner.name, franchise_rules),
                product_id=winner.product_id,
                concept_ids=tuple(sorted({e.concept_id for e in entries if e.concept_id})),
                winning_entitlement_id=winner.entitlement_id,
                active=any(e.active is not False for e in entries),
                winning_title_id=winner.title_id,
                platforms=tuple(sorted({platform for e in entries for platform in e.platforms})),
            )
        )

    return sorted(canonical, key=lambda g: g.canonical_title.lower())
