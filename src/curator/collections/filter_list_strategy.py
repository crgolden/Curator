"""Unconstrained genre/score/tier filter list, driven by a
:class:`~curator.collections.collection_spec.CollectionSpec` filter.
"""

from __future__ import annotations

from curator.collections.collection_spec import CollectionSpec
from curator.collections.filter_predicate import evaluate as evaluate_predicate
from curator.collections.game_candidate import GameCandidate
from curator.collections.sort_order import resolve_sort_key


def filter_candidates(
    candidates: list[GameCandidate], spec: CollectionSpec, *, completion_available: bool = False
) -> list[GameCandidate]:
    """Apply a ``filter_list`` spec's genre/score/tier/predicate/completion filters, unsorted.

    Split out from :func:`apply_filter_list` so :class:`~curator.collections.collection_orchestrator
    .CollectionOrchestrator` can also run just the filtering half against a ``capacity_fill`` spec before
    packing -- capacity_fill previously ignored ``filter_predicate``/``genre_filter``/``min_score``/
    ``aaa_tier_filter`` entirely, which is also why reproducing the legacy PS4 Criterion/Blockbuster
    classifier (both genre-filtered *and* capacity-packed in the same run) needed this split.

    :param candidates: Every eligible game.
    :param spec: The filter spec. When ``spec.filter_predicate`` is set, it replaces
        ``genre_filter``/``min_score``/``aaa_tier_filter`` entirely (they are not combined) -- see
        :mod:`curator.collections.filter_predicate`'s module docstring.
    :param completion_available: Whether trophy-completion data could actually be fetched for this run --
        see :func:`apply_filter_list`'s own parameter of the same name for the full rationale.
    :returns: The matching candidates, in ``candidates``' original order (unsorted).
    """
    result = candidates
    if spec.filter_predicate is not None:
        result = [candidate for candidate in result if evaluate_predicate(spec.filter_predicate, candidate)]
    else:
        if spec.genre_filter:
            allowed = {genre.lower() for genre in spec.genre_filter}
            result = [candidate for candidate in result if candidate.genre.lower() in allowed]
        if spec.min_score is not None:
            result = [
                candidate
                for candidate in result
                if candidate.composite_score is not None and candidate.composite_score >= spec.min_score
            ]
        if spec.aaa_tier_filter is not None:
            result = [candidate for candidate in result if candidate.aaa_tier == spec.aaa_tier_filter]
    if spec.min_percent_completed is not None and completion_available:
        result = [
            candidate
            for candidate in result
            if candidate.percent_completed is not None and candidate.percent_completed >= spec.min_percent_completed
        ]

    return result


def apply_filter_list(
    candidates: list[GameCandidate], spec: CollectionSpec, *, completion_available: bool = False
) -> list[GameCandidate]:
    """Filter and sort candidates per a ``filter_list`` spec. No capacity limit.

    :param candidates: Every eligible game.
    :param spec: The filter spec (``spec.kind`` is ignored here -- the caller has already decided this is
        a ``filter_list`` run). See :func:`filter_candidates` for the filtering rules and
        ``spec.sort_order`` for the sort (:func:`curator.collections.sort_order.resolve_sort_key`; unset
        keeps this function's own long-standing default, composite score descending with rank score as
        tiebreak).
    :param completion_available: Whether trophy-completion data could actually be fetched for this run.
        ``spec.min_percent_completed`` is only
        applied when this is ``True`` -- otherwise a PSN outage, a disabled ``harvest_trophies`` preference,
        or a broken link would leave every candidate's ``percent_completed`` as ``None`` and the predicate
        would exclude everything, silently turning a saved collection empty for reasons unrelated to the
        collection itself. Callers pass ``False`` (the default) whenever they haven't checked. Applied on
        top of either the flat fields or ``filter_predicate`` -- it is never part of the tree itself.
    :returns: The matching candidates, sorted per ``spec.sort_order`` (descending).
    """
    result = filter_candidates(candidates, spec, completion_available=completion_available)
    key = resolve_sort_key(
        spec.sort_order, default=lambda candidate: (candidate.composite_score or 0.0, candidate.rank_score)
    )
    return sorted(result, key=key, reverse=True)
