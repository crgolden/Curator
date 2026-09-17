"""The canonical composite/rank score -- the single call site every consumer (collections/capacity-fill,
filter-list, any future dashboard) shares. Platform-agnostic: the same three-source average applies to
every platform.
"""

from __future__ import annotations

HIGH_COMPOSITE_THRESHOLD = 85.0
MID_COMPOSITE_THRESHOLD = 75.0
HIGH_COMPOSITE_POINTS = 3
MID_COMPOSITE_POINTS = 1
FRANCHISE_POINTS = 1
F2P_PENALTY_POINTS = 3


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


def rank_score(composite: float | None, franchise: str | None, *, is_free_to_play: bool) -> int:
    """Score a game for rotation/assignment ranking.

    :param composite: The game's :func:`composite_score`.
    :param franchise: The game's assigned franchise; any non-empty value counts.
    :param is_free_to_play: Whether the storefront prices the game as free; a free game loses
        ``F2P_PENALTY_POINTS``. A paid live-service game is not penalized.
    :returns: The point total, from the module's ``*_POINTS``/``*_THRESHOLD`` constants.
    """
    points = 0

    if composite is not None:
        if composite >= HIGH_COMPOSITE_THRESHOLD:
            points += HIGH_COMPOSITE_POINTS
        elif composite >= MID_COMPOSITE_THRESHOLD:
            points += MID_COMPOSITE_POINTS

    if franchise:
        points += FRANCHISE_POINTS

    if is_free_to_play:
        points -= F2P_PENALTY_POINTS

    return points
