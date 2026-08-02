"""Given a user + a :class:`~curator.collections.collection_spec.CollectionSpec`, produces a
ranked/filtered/(optionally capacity-)packed result set on demand.

The single orchestrator both console-checklist generation ("give me what fits on this console") and
unconstrained filter lists ("all RPGs above 80") go through -- replacing ``ps_assign_ps5.py``/
``ps_assign_ps4.py``'s two hardcoded scripts with one reusable, on-the-fly pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass

from curator.collections.capacity_fill_strategy import StorageBin, fill_capacity_multi_bin
from curator.collections.collection_spec import CollectionSpec
from curator.collections.filter_list_strategy import apply_filter_list
from curator.collections.game_candidate import GameCandidate
from curator.collections.repository import CollectionsRepository, RawCandidateRow
from curator.scoring.scoring_service import composite_score, rank_score
from curator.scoring.size_estimation_service import SizeEstimate, estimate_install_size_gb

_DEFAULT_SIZE_GB = 20.0


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """One collection-generation run's outcome."""

    included: tuple[GameCandidate, ...]
    excluded: tuple[GameCandidate, ...]
    used_gb: float | None


class CollectionOrchestrator:
    """Composes candidate loading, scoring, and strategy selection into one on-demand collection run.

    :param repository: The collections repository (consoles + candidate pool reads).
    """

    def __init__(self, repository: CollectionsRepository) -> None:
        self._repository = repository

    async def generate(
        self,
        identity_sub: str,
        spec: CollectionSpec,
        *,
        size_estimates: list[SizeEstimate],
        completion_map: dict[str, int] | None = None,
        completion_available: bool = False,
    ) -> CollectionResult:
        """Generate a collection for one user from a spec.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :param spec: The collection spec (saved definition or inline preview).
        :param size_estimates: Every install-size estimate row, used when a game has no measured actual.
        :param completion_map: ``{game_id: percent_completed}``, precomputed by the caller (see
            ``curator.psn.trophy_completion.get_completion_result``) -- attached to each
            :class:`~curator.collections.game_candidate.GameCandidate` for display; ``None``/missing
            entries leave ``percent_completed`` as ``None``.
        :param completion_available: Whether ``completion_map`` reflects a real fetch attempt (as opposed
            to trophy data being unavailable for this user right now) -- see
            ``curator.collections.filter_list_strategy.apply_filter_list``'s ``completion_available``
            parameter, which this is passed straight through to.
        :returns: The :class:`CollectionResult`.
        :raises ValueError: If ``spec.kind == "capacity_fill"`` and ``console_id`` is missing or unknown.
        """
        platform: str | None = None
        bins: list[StorageBin] = []
        routing_genres: tuple[str, ...] = ()

        if spec.kind == "capacity_fill":
            if spec.console_id is None:
                raise ValueError("capacity_fill requires a console_id")
            consoles = await self._repository.list_user_consoles(identity_sub)
            console = next((c for c in consoles if c.console_id == spec.console_id), None)
            if console is None:
                raise ValueError(f"Unknown console_id {spec.console_id!r} for this user")
            platform = console.platform
            routing_genres = console.routing_genres

            # Console-internal storage first, then each currently-attached device, in the order
            # list_storage_devices already returns (by name) -- first-fit tries bins in this order.
            # A kind="usb" device is never offered to a PS5 run at all: a PS5 title cannot run from
            # external USB storage (curator.storage_devices_routes enforces the same rule at install
            # time), and "not in the candidate pool" is a stronger guarantee than "filtered out after".
            bins = [StorageBin(bin_id=console.console_id, capacity_gb=console.effective_capacity_gb)]
            attached_devices = [
                device
                for device in await self._repository.list_storage_devices(identity_sub)
                if device.console_id == spec.console_id and (platform != "PS5" or device.kind != "usb")
            ]
            bins.extend(
                StorageBin(bin_id=device.device_id, capacity_gb=device.effective_capacity_gb)
                for device in attached_devices
            )

        raw_rows = await self._repository.list_candidates(
            identity_sub,
            platform=platform,
            include_inactive=spec.include_inactive,
            min_percent_completed=spec.min_percent_completed,
        )
        # Each row already carries its stored trophy percentage, and the completion floor was applied in
        # SQL above. completion_map is only an override for a caller that resolved fresher numbers than
        # the persisted ones; absent it, the row's own value stands.
        completion_map = completion_map or {}
        candidates = [
            self._score(
                row,
                size_estimates,
                is_ps5=(platform == "PS5"),
                percent_completed=completion_map.get(row.game_id, row.percent_completed),
            )
            for row in raw_rows
        ]

        if spec.kind == "capacity_fill":
            fill_result = fill_capacity_multi_bin(candidates, bins, routing_genres=routing_genres)
            # Flattened back into one included/used_gb pair, in bin order (console-internal first, then
            # each attached device) -- CollectionResult's external shape is unchanged by going multi-bin
            # internally; which specific bin a recommended game landed on isn't surfaced today (nothing
            # downstream reads it), so this stays additive rather than a breaking response-shape change.
            included = tuple(
                candidate for storage_bin in bins for candidate in fill_result.installed_by_bin[storage_bin.bin_id]
            )
            used_gb = sum(fill_result.used_gb_by_bin.values())
            return CollectionResult(included=included, excluded=fill_result.overflow, used_gb=used_gb)

        filtered = apply_filter_list(candidates, spec, completion_available=completion_available)
        included_ids = {candidate.game_id for candidate in filtered}
        excluded = tuple(candidate for candidate in candidates if candidate.game_id not in included_ids)
        return CollectionResult(included=tuple(filtered), excluded=excluded, used_gb=None)

    @staticmethod
    def _score(
        row: RawCandidateRow,
        size_estimates: list[SizeEstimate],
        *,
        is_ps5: bool,
        percent_completed: int | None = None,
    ) -> GameCandidate:
        comp = composite_score(row.critical_score, row.oc_score, row.psn_rating)
        # game_enrichment.is_free_to_play is a clean boolean (the schema fix that replaced the legacy
        # pipeline's free-text Multiplayer keyword-match smell) -- rank_score()'s signature still takes a
        # free-text descriptor (ported faithfully from ps_assign_ps5.py), so synthesize the minimal text
        # its F2P keyword check needs rather than changing that already-shipped, already-tested function.
        multiplayer_text = "free to play" if row.is_free_to_play else ""
        points = rank_score(comp, multiplayer_text, row.franchise)
        size_gb = row.measured_size_gb
        if size_gb is None:
            size_gb = estimate_install_size_gb(
                row.title, row.genre or "", is_ps5, row.aaa_tier or "Indie", size_estimates
            )
        if size_gb is None:
            size_gb = _DEFAULT_SIZE_GB
        return GameCandidate(
            game_id=row.game_id,
            title=row.title,
            genre=row.genre or "",
            aaa_tier=row.aaa_tier or "Indie",
            franchise=row.franchise or "",
            composite_score=comp,
            rank_score=points,
            size_gb=float(size_gb),
            percent_completed=percent_completed,
        )
