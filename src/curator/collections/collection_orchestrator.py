"""Given a user + a :class:`~curator.collections.collection_spec.CollectionSpec`, produces a
ranked/filtered/(optionally capacity-)packed result set on demand.

The single orchestrator both console-checklist generation ("give me what fits on this console") and
unconstrained filter lists ("all RPGs above 80") go through.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from curator.collections.capacity_fill_strategy import StorageBin, fill_capacity_multi_bin
from curator.collections.collection_spec import CollectionSpec
from curator.collections.filter_list_strategy import apply_filter_list, filter_candidates
from curator.collections.game_candidate import (
    DEFAULT_SIZE,
    ESTIMATED_SIZE,
    MEASURED_SIZE,
    GameCandidate,
    SizeSource,
)
from curator.collections.repository import CollectionsRepository, RawCandidateRow
from curator.psn.title_platform import ConsolePlatform
from curator.scoring.scoring_service import composite_score, rank_score
from curator.scoring.size_estimation_service import SizeEstimate, estimate_install_size_gb

_DEFAULT_SIZE_GB = 20.0

_NO_CONSOLE_ESTIMATE_PLATFORM: ConsolePlatform = "PS4"
"""Which platform's size band a collection with **no console attached** estimates against.

A ``filter_list`` collection is not bound to a console, so it has no platform of its own, and the packing
step it feeds still needs a size. PS4 is the conservative half of the pair ``0033_seed_size_estimates``
actually seeds, and it is what this specific path already resolved to.

It does **not** cover a console on a platform with no seeded bands. A PS3, Vita, PSP, PS2 or PS1
``capacity_fill`` estimates as that platform, finds nothing in ``size_estimates``, and falls to
:data:`_DEFAULT_SIZE_GB` rather than borrowing PS4's figures -- see
``curator.scoring.size_estimation_service.estimate_install_size_gb``, which returns ``None`` deliberately.
Reporting a PS3 disc as a 64 GB PS4 open-world title would be a worse answer than a flat one, and seeding
real bands for those platforms needs published figures nobody has measured.
"""


@dataclass(frozen=True, slots=True)
class CollectionResult:
    """One collection-generation run's outcome.

    :param unmatched: ``capacity_fill`` only -- candidates that didn't satisfy the spec's own
        genre/score/tier/predicate filter at all, as distinct from :attr:`excluded` (candidates that
        *did* match but didn't fit the capacity budget). Always empty for ``filter_list``, where
        :attr:`excluded` already means exactly this (no capacity constraint to separately overflow on).
        This is what lets a caller compose the legacy PS4 Criterion/Blockbuster cascade -- "Blockbuster's
        input is its own candidates plus Criterion's capacity overflow plus whatever matched neither" --
        by feeding ``excluded + unmatched`` game ids into a second :meth:`CollectionOrchestrator.generate`
        call's ``chained_candidate_ids``. Not persisted or exposed over HTTP; see that parameter's
        docstring for why chaining stays an engine-level composition, not a saved/API concept yet.
    """

    included: tuple[GameCandidate, ...]
    excluded: tuple[GameCandidate, ...]
    used_gb: float | None
    unmatched: tuple[GameCandidate, ...] = ()


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
        chained_candidate_ids: Sequence[str] | None = None,
    ) -> CollectionResult:
        """Generate a collection for one user from a spec.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :param spec: The collection spec (saved definition or inline preview).
        :param size_estimates: Every install-size estimate row, used when a game has no measured actual.
        :param completion_map: ``{game_id: percent_completed}``, precomputed by the caller -- attached to each
            :class:`~curator.collections.game_candidate.GameCandidate` for display; ``None``/missing
            entries leave ``percent_completed`` as ``None``.
        :param completion_available: Whether ``completion_map`` reflects a real fetch attempt (as opposed
            to trophy data being unavailable for this user right now) -- see
            ``curator.collections.filter_list_strategy.apply_filter_list``'s ``completion_available``
            parameter, which this is passed straight through to.
        :param chained_candidate_ids: ``capacity_fill`` only -- game ids (typically a prior
            :meth:`generate` call's ``excluded + unmatched``, see :class:`CollectionResult`) added to
            this run's packing pool *unconditionally*, bypassing this spec's own genre/score/tier/
            predicate filter entirely, alongside whatever normally matches it. Order matters and is
            preserved: with ``sort_order`` set to a variant that has ties (e.g. ``"composite_desc"``,
            which drops ``rank_score`` as a tiebreak), the stable sort's tie-break falls back to this
            sequence's own order for the items it contributes, the same way the legacy PS4 script's
            ``blockbuster_candidates + criterion_overflow + uncategorised`` concatenation order decided
            its own ties -- pass ``excluded`` before ``unmatched`` (not, say, a plain set) to match that
            precedent. This is deliberately not a persisted or API-facing concept -- it exists so a
            caller (today, a one-off reproduction script; ``Tools/PlayStation/LIFECYCLE_AUDIT.md``) can
            compose the legacy PS4 Criterion→Blockbuster cascade without Curator inventing a saved
            collection-to-collection dependency graph (cycle rules, edit/delete semantics for a shape no
            UI has been designed for yet -- an undesigned feature does not get built ahead of a design).
            A game id present in both ``chained_candidate_ids`` and this spec's own normal
            match is only counted once (kept in its normal-match position, not moved to the chained
            position).
        :returns: The :class:`CollectionResult`.
        :raises ValueError: If ``spec.kind == "capacity_fill"`` and ``console_id`` is missing or unknown,
            or if ``spec.exclude_installed_on`` names a console that isn't this caller's own.
        """
        platform: ConsolePlatform | None = None
        bins: list[StorageBin] = []
        routing_genres: tuple[str, ...] = ()

        consoles = None
        if spec.kind == "capacity_fill" or spec.exclude_installed_on:
            consoles = await self._repository.list_user_consoles(identity_sub)

        if spec.exclude_installed_on:
            assert consoles is not None
            owned_console_ids = {console.console_id for console in consoles}
            unknown = [cid for cid in spec.exclude_installed_on if cid not in owned_console_ids]
            if unknown:
                raise ValueError(f"Unknown console_id(s) for this user in exclude_installed_on: {unknown!r}")

        if spec.kind == "capacity_fill":
            if spec.console_id is None:
                raise ValueError("capacity_fill requires a console_id")
            assert consoles is not None
            console = next((c for c in consoles if c.console_id == spec.console_id), None)
            if console is None:
                raise ValueError(f"Unknown console_id {spec.console_id!r} for this user")
            platform = console.platform
            routing_genres = console.routing_genres

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
            exclude_installed_on=spec.exclude_installed_on,
        )
        completion_map = completion_map or {}
        candidates = [
            self._score(
                row,
                size_estimates,
                platform=platform,
                percent_completed=completion_map.get(row.game_id, row.percent_completed),
            )
            for row in raw_rows
        ]

        if spec.kind == "capacity_fill":
            matched = filter_candidates(candidates, spec, completion_available=completion_available)
            matched_ids = {candidate.game_id for candidate in matched}
            by_id = {candidate.game_id: candidate for candidate in candidates}
            chained_ids_set = frozenset(chained_candidate_ids) if chained_candidate_ids else frozenset()
            chained: list[GameCandidate] = []
            seen_chained_ids: set[str] = set()
            for game_id in chained_candidate_ids or ():
                if game_id in matched_ids or game_id in seen_chained_ids:
                    continue
                candidate = by_id.get(game_id)
                if candidate is not None:
                    chained.append(candidate)
                    seen_chained_ids.add(game_id)
            unmatched = tuple(
                candidate
                for candidate in candidates
                if candidate.game_id not in matched_ids and candidate.game_id not in chained_ids_set
            )
            fill_result = fill_capacity_multi_bin(
                matched + chained, bins, routing_genres=routing_genres, sort_order=spec.sort_order
            )
            included = tuple(
                candidate for storage_bin in bins for candidate in fill_result.installed_by_bin[storage_bin.bin_id]
            )
            used_gb = sum(fill_result.used_gb_by_bin.values())
            return CollectionResult(
                included=included, excluded=fill_result.overflow, used_gb=used_gb, unmatched=unmatched
            )

        filtered = apply_filter_list(candidates, spec, completion_available=completion_available)
        included_ids = {candidate.game_id for candidate in filtered}
        excluded = tuple(candidate for candidate in candidates if candidate.game_id not in included_ids)
        return CollectionResult(included=tuple(filtered), excluded=excluded, used_gb=None)

    @staticmethod
    def _score(
        row: RawCandidateRow,
        size_estimates: list[SizeEstimate],
        *,
        platform: ConsolePlatform | None,
        percent_completed: int | None = None,
    ) -> GameCandidate:
        comp = composite_score(row.critical_score, row.oc_score, row.psn_rating)
        multiplayer_text = "free to play" if row.is_free_to_play else ""
        points = rank_score(comp, multiplayer_text, row.franchise)
        size_gb, size_source = CollectionOrchestrator._resolve_size(row, size_estimates, platform=platform)
        return GameCandidate(
            game_id=row.game_id,
            title=row.title,
            genre=row.genre or "",
            aaa_tier=row.aaa_tier,
            franchise=row.franchise or "",
            composite_score=comp,
            rank_score=points,
            size_gb=size_gb,
            percent_completed=percent_completed,
            size_source=size_source,
        )

    @staticmethod
    def _resolve_size(
        row: RawCandidateRow, size_estimates: list[SizeEstimate], *, platform: ConsolePlatform | None
    ) -> tuple[float, SizeSource]:
        """The install-size ladder, and which rung answered.

        Contributed measurement, then a ``size_estimates`` band, then :data:`_DEFAULT_SIZE_GB`. The rung
        travels with the size because a bare number cannot tell a caller whether 20 GB is a fact somebody
        measured or the flat fallback standing in for one -- the distinction ``GET``/``PUT
        /games/{game_id}/measured-sizes`` exists to let a user close.
        """
        if row.measured_size_gb is not None:
            return float(row.measured_size_gb), MEASURED_SIZE

        estimated_gb = estimate_install_size_gb(
            row.title,
            row.genre or "",
            platform or _NO_CONSOLE_ESTIMATE_PLATFORM,
            row.aaa_tier or "Indie",
            size_estimates,
        )
        if estimated_gb is not None:
            return float(estimated_gb), ESTIMATED_SIZE

        return _DEFAULT_SIZE_GB, DEFAULT_SIZE
