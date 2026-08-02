"""The spec driving one collection-generation run -- either a saved ``collection_definitions`` row or an
inline spec supplied to ``POST /collections/preview``.
"""

from __future__ import annotations

from dataclasses import dataclass

from curator.collections.filter_predicate import FilterPredicate


@dataclass(frozen=True, slots=True)
class CollectionSpec:
    """One collection-generation request.

    :param kind: ``"capacity_fill"`` (bin-pack against a specific console's effective capacity) or
        ``"filter_list"`` (unconstrained genre/score/tier filter).
    :param console_id: Required for ``"capacity_fill"``; ignored for ``"filter_list"``.
    :param genre_filter: For ``"filter_list"``: only these genres (case-insensitive) are included; empty
        means no genre restriction. Ignored when ``filter_predicate`` is set (see below).
    :param min_score: For ``"filter_list"``: minimum composite score to include; ``None`` means no floor.
        Ignored when ``filter_predicate`` is set.
    :param aaa_tier_filter: For ``"filter_list"``: restrict to this publisher tier; ``None`` means no
        restriction. Ignored when ``filter_predicate`` is set.
    :param filter_predicate: For ``"filter_list"``: an OR-capable predicate tree (WP8) over genre/tier/
        score, evaluated by :mod:`curator.collections.filter_predicate` instead of ``genre_filter``/
        ``min_score``/``aaa_tier_filter`` when present -- the flat fields are pure AND and cannot express
        e.g. "genre in SET or (genre == action and tier == indie)". ``None`` (the default) keeps today's
        flat-field behavior unchanged.
    :param min_percent_completed: For ``"filter_list"``: minimum trophy completion percentage to include;
        ``None`` means no floor. Applied by :func:`~curator.collections.filter_list_strategy.apply_filter_list`
        only when completion data was actually available for this run (see that function's docstring) --
        a run during a PSN outage or with trophy harvesting disabled must not silently exclude everything.
        Always applied on top of either the flat fields or ``filter_predicate`` -- see
        :mod:`curator.collections.filter_predicate`'s module docstring for why it stays out of the tree.
    :param sort_order: Reserved for future sort variants; the only sort implemented today is composite
        score descending (both strategies already do this).
    :param include_inactive: Whether to draw on games the user can no longer play (lapsed PS Plus titles
        and the like). Defaults to ``False`` -- a collection is normally built from what its owner can
        actually launch -- and opting in gives "everything I ever had access to" instead. Applied in
        :meth:`~curator.collections.repository.CollectionsRepository.list_candidates`, the single
        chokepoint every strategy's candidate pool flows through, rather than in each strategy.
    """

    kind: str
    console_id: str | None = None
    genre_filter: tuple[str, ...] = ()
    min_score: float | None = None
    aaa_tier_filter: str | None = None
    sort_order: str | None = None
    include_inactive: bool = False
    min_percent_completed: int | None = None
    filter_predicate: FilterPredicate | None = None
