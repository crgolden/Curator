"""Unconstrained genre/score/tier filter list, generalized from the hardcoded "Criterion"/"Blockbuster"
genre-set classifiers in ``ps_assign_ps4.py`` -- replaced by a data-driven
:class:`~curator.collections.collection_spec.CollectionSpec` filter instead of two fixed genre sets.
"""

from __future__ import annotations

from curator.collections.collection_spec import CollectionSpec
from curator.collections.game_candidate import GameCandidate


def apply_filter_list(candidates: list[GameCandidate], spec: CollectionSpec) -> list[GameCandidate]:
    """Filter and sort candidates per a ``filter_list`` spec. No capacity limit.

    :param candidates: Every eligible game.
    :param spec: The filter spec (``spec.kind`` is ignored here -- the caller has already decided this is
        a ``filter_list`` run).
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

    return sorted(result, key=lambda candidate: (candidate.composite_score or 0.0, candidate.rank_score), reverse=True)
