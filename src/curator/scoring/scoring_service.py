"""The canonical composite/rank score, ported from ``ps_assign_ps5.py``'s ``composite_score()``/
``rank_score()`` -- the single call site every consumer (collections/capacity-fill, filter-list, any
future dashboard) shares.

Unifying this closes a real drift that existed in the legacy pipeline: ``ps_assign_ps4.py`` computed
``composite_score()`` without a PSN-rating input at all (an accepted, intentional behavior change per the
migration plan -- PS4 assignment now gets the same three-source average PS5 assignment always had).
"""

from __future__ import annotations

F2P_KEYWORDS = frozenset({"free to play", "f2p", "live service", "live-service", "free-to-play"})


def composite_score(
    critical_score: float | None, oc_score: float | None, psn_rating: float | None = None
) -> float | None:
    """Average whichever of (critic score, OpenCritic score, PSN star rating) are available.

    :param critical_score: RAWG's Metacritic-sourced score (0-100), or ``None``.
    :param oc_score: OpenCritic's top-critic score (0-100), or ``None``.
    :param psn_rating: The PSN Store's 1-5 star rating, or ``None`` -- normalized to a 0-100 scale
        (``(stars - 1) / 4 * 100``) before averaging with the critic scores.
    :returns: The average of the available scores, or ``None`` if none are available.
    """
    normalized_psn = round((psn_rating - 1) / 4 * 100, 1) if psn_rating is not None else None
    scores = [score for score in (critical_score, oc_score, normalized_psn) if score is not None]
    return sum(scores) / len(scores) if scores else None


def rank_score(composite: float | None, multiplayer: str | None, franchise: str | None) -> int:
    """Score a game for rotation/assignment ranking.

    :param composite: The game's :func:`composite_score`.
    :param multiplayer: The game's multiplayer/live-service descriptor text (checked for F2P keywords).
    :param franchise: The game's assigned franchise (see
        :func:`~curator.catalog.franchise_assigner.assign_franchise`); any non-empty value counts.
    :returns: The point total: ``+3`` for composite >= 85, ``+1`` for composite 75-84, ``+1`` if part of a
        franchise, ``-3`` if tagged free-to-play/live-service.
    """
    points = 0

    if composite is not None:
        if composite >= 85:
            points += 3
        elif composite >= 75:
            points += 1

    if franchise:
        points += 1

    multiplayer_lower = (multiplayer or "").lower()
    if any(keyword in multiplayer_lower for keyword in F2P_KEYWORDS):
        points -= 3

    return points
