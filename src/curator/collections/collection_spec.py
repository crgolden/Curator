"""The spec driving one collection-generation run -- either a saved ``collection_definitions`` row or an
inline spec supplied to ``POST /collections/preview``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CollectionSpec:
    """One collection-generation request.

    :param kind: ``"capacity_fill"`` (bin-pack against a specific console's effective capacity) or
        ``"filter_list"`` (unconstrained genre/score/tier filter).
    :param console_id: Required for ``"capacity_fill"``; ignored for ``"filter_list"``.
    :param genre_filter: For ``"filter_list"``: only these genres (case-insensitive) are included; empty
        means no genre restriction.
    :param min_score: For ``"filter_list"``: minimum composite score to include; ``None`` means no floor.
    :param aaa_tier_filter: For ``"filter_list"``: restrict to this publisher tier; ``None`` means no
        restriction.
    :param sort_order: Reserved for future sort variants; the only sort implemented today is composite
        score descending (both strategies already do this).
    """

    kind: str
    console_id: str | None = None
    genre_filter: tuple[str, ...] = ()
    min_score: float | None = None
    aaa_tier_filter: str | None = None
    sort_order: str | None = None
