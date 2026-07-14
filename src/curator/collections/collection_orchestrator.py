"""Given a user + a :class:`~curator.collections.collection_spec.CollectionSpec`, produces a
ranked/filtered/(optionally capacity-)packed result set on demand.

The single orchestrator both console-checklist generation ("give me what fits on this console") and
unconstrained filter lists ("all RPGs above 80") go through -- replacing ``ps_assign_ps5.py``/
``ps_assign_ps4.py``'s two hardcoded scripts with one reusable, on-the-fly pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass

from curator.collections.capacity_fill_strategy import fill_capacity
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
    ) -> CollectionResult:
        """Generate a collection for one user from a spec.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :param spec: The collection spec (saved definition or inline preview).
        :param size_estimates: Every install-size estimate row, used when a game has no measured actual.
        :returns: The :class:`CollectionResult`.
        :raises ValueError: If ``spec.kind == "capacity_fill"`` and ``console_id`` is missing or unknown.
        """
        platform: str | None = None
        capacity_gb: float | None = None
        routing_genres: tuple[str, ...] = ()

        if spec.kind == "capacity_fill":
            if spec.console_id is None:
                raise ValueError("capacity_fill requires a console_id")
            consoles = await self._repository.list_user_consoles(identity_sub)
            console = next((c for c in consoles if c.console_id == spec.console_id), None)
            if console is None:
                raise ValueError(f"Unknown console_id {spec.console_id!r} for this user")
            platform = console.platform
            capacity_gb = console.effective_capacity_gb
            routing_genres = console.routing_genres

        raw_rows = await self._repository.list_candidates(identity_sub, platform=platform)
        candidates = [self._score(row, size_estimates, is_ps5=(platform == "PS5")) for row in raw_rows]

        if spec.kind == "capacity_fill":
            assert capacity_gb is not None  # guaranteed by the branch above
            fill_result = fill_capacity(candidates, capacity_gb, routing_genres=routing_genres)
            return CollectionResult(
                included=fill_result.installed, excluded=fill_result.overflow, used_gb=fill_result.used_gb
            )

        filtered = apply_filter_list(candidates, spec)
        included_ids = {candidate.game_id for candidate in filtered}
        excluded = tuple(candidate for candidate in candidates if candidate.game_id not in included_ids)
        return CollectionResult(included=tuple(filtered), excluded=excluded, used_gb=None)

    @staticmethod
    def _score(row: RawCandidateRow, size_estimates: list[SizeEstimate], *, is_ps5: bool) -> GameCandidate:
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
        )
