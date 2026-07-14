"""Pure trophy-derivation logic, with no I/O of its own.

Extracted from ``psnpy.client.PsnAgent.rarest_trophies_for_title``, which originally called
``title_trophies`` itself and then derived the rarest subset in the same method. Splitting the derivation
out means it's independently unit-testable against a canned list of :class:`~curator.psn.models.TrophyDetail`
-- no fake session or client needed at all.
"""

from __future__ import annotations

from curator.psn.models import TrophyDetail


def rarest_trophies(trophies: list[TrophyDetail], limit: int = 10) -> list[TrophyDetail]:
    """Return a title's rarest trophies (lowest earn-rate percentage), rarest first.

    PSN's rarest-trophies data is itself just this same per-trophy rarity (``trophyEarnRate``), sorted and
    truncated. Trophies with unknown rarity are excluded, since sort order for them is undefined.

    :param trophies: Trophy details for a title (e.g. from
        :meth:`~curator.psn.trophy_client.TrophyClient.title_trophies`).
    :param limit: Maximum number of trophies to return.
    :returns: The rarest trophies, ascending rarity, truncated to ``limit``.
    """
    rated = [trophy for trophy in trophies if trophy.rarity is not None]

    def _rarity(trophy: TrophyDetail) -> float:
        assert trophy.rarity is not None  # guaranteed by the filter above
        return trophy.rarity

    rated.sort(key=_rarity)
    return rated[:limit]
