"""Console-capacity-constrained, multi-bin bin-pack: a console's own built-in storage and each of its
currently-attached :class:`~curator.collections.repository.StorageDevice` rows are separate capacity
pools, never pooled into one combined number. Console-internal and an attached M.2 drive are two
different bins that happen to allow the same titles, while an attached USB drive is a third bin that
allows fewer -- they share only the playability rule, never a pool.

Multi-console overflow spilling across several *consoles* remains a
:mod:`curator.collections.collection_orchestrator` concern, not this module's -- this only packs across
the bins that belong to *one* console-generation run.
"""

from __future__ import annotations

from dataclasses import dataclass

from curator.collections.game_candidate import GameCandidate
from curator.collections.sort_order import resolve_sort_key


@dataclass(frozen=True, slots=True)
class StorageBin:
    """One packable capacity pool within a single console-generation run: the console's own built-in
    storage, or one of its currently-attached storage devices.

    :param bin_id: The console's own ``console_id`` for its built-in storage, or a device's ``device_id``
        for an attached :class:`~curator.collections.repository.StorageDevice` -- opaque to this module,
        just a key for :attr:`MultiBinFillResult.installed_by_bin`/``used_gb_by_bin``.
    :param capacity_gb: This bin's own effective capacity (already buffer-subtracted by the caller -- see
        ``UserConsole.effective_capacity_gb``/``StorageDevice.effective_capacity_gb``).
    """

    bin_id: str
    capacity_gb: float


@dataclass(frozen=True, slots=True)
class MultiBinFillResult:
    """One multi-bin capacity-fill run's outcome.

    :param installed_by_bin: Which candidates landed in each bin, keyed by :attr:`StorageBin.bin_id`, in
        the same highest-rank-first order :func:`fill_capacity_multi_bin` placed them.
    :param overflow: Every candidate that didn't fit in any bin, in rank order.
    :param used_gb_by_bin: How much of each bin's capacity is now used, keyed by :attr:`StorageBin.bin_id`.
    """

    installed_by_bin: dict[str, tuple[GameCandidate, ...]]
    overflow: tuple[GameCandidate, ...]
    used_gb_by_bin: dict[str, float]


def fill_capacity_multi_bin(
    candidates: list[GameCandidate],
    bins: list[StorageBin],
    *,
    routing_genres: tuple[str, ...] = (),
    sort_order: str | None = None,
) -> MultiBinFillResult:
    """Greedily fill a set of capacity bins, highest-ranked first, first-fit across bins in the order
    given.

    Each candidate is offered to ``bins`` in list order and placed in the first one with enough remaining
    capacity; a candidate that fits nowhere goes to ``overflow``. The caller decides both bin order (e.g.
    console-internal before an attached device) and bin *membership* -- in particular, whether a
    ``kind="usb"`` device's bin belongs in ``bins`` at all for this run, since a PS5 title's candidate
    pool must never be offered USB storage at all (see ``curator.storage_devices_routes``'s install-time
    rejection of the same rule). This function has no platform awareness of its own; it only packs.

    :param candidates: Every eligible game (already filtered to whatever platform-eligibility applies).
    :param bins: Every capacity pool to pack into, in the order they should be tried.
    :param routing_genres: If non-empty, only candidates whose genre is in this set are considered; the
        rest are excluded from both ``installed_by_bin`` and ``overflow`` entirely -- a caller doing
        multi-console routing decides what happens to them (e.g. offering them to another console).
    :param sort_order: See :func:`curator.collections.sort_order.resolve_sort_key`. ``None`` keeps this
        function's own long-standing default, ``(rank_score, composite_score)`` descending.
    :returns: The :class:`MultiBinFillResult`.
    """
    pool = candidates
    if routing_genres:
        allowed = {genre.lower() for genre in routing_genres}
        pool = [candidate for candidate in pool if candidate.genre.lower() in allowed]

    key = resolve_sort_key(
        sort_order, default=lambda candidate: (candidate.rank_score, candidate.composite_score or 0.0)
    )
    ranked = sorted(pool, key=key, reverse=True)

    remaining_gb = {b.bin_id: b.capacity_gb for b in bins}
    installed: dict[str, list[GameCandidate]] = {b.bin_id: [] for b in bins}
    overflow: list[GameCandidate] = []

    for candidate in ranked:
        for b in bins:
            if remaining_gb[b.bin_id] >= candidate.size_gb:
                installed[b.bin_id].append(candidate)
                remaining_gb[b.bin_id] -= candidate.size_gb
                break
        else:
            overflow.append(candidate)

    return MultiBinFillResult(
        installed_by_bin={bin_id: tuple(games) for bin_id, games in installed.items()},
        overflow=tuple(overflow),
        used_gb_by_bin={b.bin_id: b.capacity_gb - remaining_gb[b.bin_id] for b in bins},
    )
