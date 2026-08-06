"""Named sort-order variants shared by both collection strategies (:mod:`curator.collections
.capacity_fill_strategy`/:mod:`curator.collections.filter_list_strategy`).

``CollectionSpec.sort_order`` is a small named allowlist, not a freeform expression parser -- there is
no general-purpose sort DSL here, only a fixed set of named variants added as real strategies need them.
``None`` (the default) is not "no sort"; it means "keep this strategy's own existing default", which is
deliberately *different* between the two strategies (``capacity_fill`` ranks by ``(rank_score,
composite_score)``, ``filter_list`` by ``(composite_score, rank_score)`` -- see each strategy's own
comment for why neither is "more correct" than the other). Reproducing the legacy PS4
Criterion/Blockbuster classifier (``Tools/PlayStation/LIFECYCLE_AUDIT.md``) is what ``"composite_desc"``
exists for: that rule ranks by composite score alone, with no ``rank_score`` (F2P/franchise bonus) term
at all, and ties broken by input order (Python's ``sorted()`` is stable, so this only holds if the
caller's input order is itself deterministic -- see ``CollectionsRepository.list_candidates``'s own
``ORDER BY`` tiebreak).
"""

from __future__ import annotations

from collections.abc import Callable

from curator.collections.game_candidate import GameCandidate

SortKey = Callable[[GameCandidate], tuple[object, ...]]

_SORT_ORDERS: dict[str, SortKey] = {
    # Not None first, then descending by value -- the same "NULLS LAST, descending" shape used
    # everywhere else scores are sorted (e.g. curator.library.repository's SQL ORDER BY columns), just
    # expressed as an in-memory key since this sort has to run before capacity-fill packing, which SQL
    # can't do.
    "composite_desc": lambda candidate: (candidate.composite_score is not None, candidate.composite_score or 0.0),
}


def resolve_sort_key(sort_order: str | None, default: SortKey) -> SortKey:
    """Return the sort key ``sort_order`` names, or ``default`` if ``sort_order`` is ``None``.

    :param sort_order: A key into :data:`_SORT_ORDERS`, or ``None`` to keep the caller's own default.
    :param default: The calling strategy's own current sort key -- returned unchanged when ``sort_order``
        is ``None``, so leaving a spec's ``sort_order`` unset changes nothing about existing behavior.
    :raises ValueError: If ``sort_order`` is set but not a known name.
    """
    if sort_order is None:
        return default
    try:
        return _SORT_ORDERS[sort_order]
    except KeyError:
        raise ValueError(f"Unknown sort_order {sort_order!r}; expected one of {sorted(_SORT_ORDERS)}") from None
