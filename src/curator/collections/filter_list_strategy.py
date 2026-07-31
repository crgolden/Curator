"""Unconstrained genre/score/tier filter list, generalized from the hardcoded "Criterion"/"Blockbuster"
genre-set classifiers in ``ps_assign_ps4.py`` -- replaced by a data-driven
:class:`~curator.collections.collection_spec.CollectionSpec` filter instead of two fixed genre sets.
"""

from __future__ import annotations

from curator.collections.collection_spec import CollectionSpec
from curator.collections.game_candidate import GameCandidate


def apply_filter_list(
    candidates: list[GameCandidate], spec: CollectionSpec, *, completion_available: bool = False
) -> list[GameCandidate]:
    """Filter and sort candidates per a ``filter_list`` spec. No capacity limit.

    :param candidates: Every eligible game.
    :param spec: The filter spec (``spec.kind`` is ignored here -- the caller has already decided this is
        a ``filter_list`` run).
    :param completion_available: Whether trophy-completion data could actually be fetched for this run (see
        ``curator.psn.trophy_completion.CompletionResult.available``). ``spec.min_percent_completed`` is only
        applied when this is ``True`` -- otherwise a PSN outage, a disabled ``harvest_trophies`` preference,
        or a broken link would leave every candidate's ``percent_completed`` as ``None`` and the predicate
        would exclude everything, silently turning a saved collection empty for reasons unrelated to the
        collection itself. Callers pass ``False`` (the default) whenever they haven't checked.
    :returns: The matching candidates, sorted by composite score descending (ties broken by rank score).
    """
    result = candidates
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

    return sorted(result, key=lambda candidate: (candidate.composite_score or 0.0, candidate.rank_score), reverse=True)
