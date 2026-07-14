"""The scored, ready-to-filter/pack unit ``curator.collections`` strategies operate on.

Built by :class:`~curator.collections.collection_orchestrator.CollectionOrchestrator` from a user's
``library_entries`` + ``game_enrichment`` + a resolved install size, using
:mod:`curator.scoring.scoring_service`'s canonical composite/rank score -- the strategies themselves never
touch raw enrichment fields or call the scoring functions directly.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GameCandidate:
    """One of a user's owned games, already scored and sized, ready for a collection strategy."""

    game_id: str
    title: str
    genre: str
    aaa_tier: str
    franchise: str
    composite_score: float | None
    rank_score: int
    size_gb: float
