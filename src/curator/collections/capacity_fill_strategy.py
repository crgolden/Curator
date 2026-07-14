"""Console-capacity-constrained bin-pack, generalized from ``ps_assign_ps5.py``/``ps_assign_ps4.py``'s
greedy fill.

Replaces the two hardcoded named drives (PS5 / PS4 Criterion / PS4 Blockbuster) with one reusable
algorithm run against ANY console's effective capacity (``raw_capacity_gb - update_buffer_gb``) and
(optionally) that console's own ``routing_genres`` -- multi-console overflow spilling across several
consoles is a :mod:`curator.collections.collection_orchestrator` concern, not this pure single-bin
function's.
"""

from __future__ import annotations

from dataclasses import dataclass

from curator.collections.game_candidate import GameCandidate


@dataclass(frozen=True, slots=True)
class CapacityFillResult:
    """One capacity-fill run's outcome."""

    installed: tuple[GameCandidate, ...]
    overflow: tuple[GameCandidate, ...]
    used_gb: float


def fill_capacity(
    candidates: list[GameCandidate],
    capacity_gb: float,
    *,
    routing_genres: tuple[str, ...] = (),
) -> CapacityFillResult:
    """Greedily fill a console up to its effective capacity, highest rank/composite score first.

    :param candidates: Every eligible game (already filtered to whatever platform-eligibility applies).
    :param capacity_gb: The console's effective capacity (``raw_capacity_gb - update_buffer_gb``).
    :param routing_genres: If non-empty, only candidates whose genre is in this set are considered; the
        rest are excluded from both ``installed`` and ``overflow`` entirely -- a caller doing
        multi-console routing decides what happens to them (e.g. offering them to another console).
    :returns: The :class:`CapacityFillResult`.
    """
    pool = candidates
    if routing_genres:
        allowed = {genre.lower() for genre in routing_genres}
        pool = [candidate for candidate in pool if candidate.genre.lower() in allowed]

    ranked = sorted(pool, key=lambda candidate: (candidate.rank_score, candidate.composite_score or 0.0), reverse=True)

    installed: list[GameCandidate] = []
    overflow: list[GameCandidate] = []
    used_gb = 0.0
    for candidate in ranked:
        if used_gb + candidate.size_gb <= capacity_gb:
            installed.append(candidate)
            used_gb += candidate.size_gb
        else:
            overflow.append(candidate)

    return CapacityFillResult(installed=tuple(installed), overflow=tuple(overflow), used_gb=used_gb)
