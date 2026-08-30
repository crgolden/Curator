"""The scored, ready-to-filter/pack unit ``curator.collections`` strategies operate on.

Built by :class:`~curator.collections.collection_orchestrator.CollectionOrchestrator` from a user's
``library_entries`` + ``game_enrichment`` + a resolved install size, using
:mod:`curator.scoring.scoring_service`'s canonical composite/rank score -- the strategies themselves never
touch raw enrichment fields or call the scoring functions directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

SizeSource = Literal["measured", "estimated", "default"]

MEASURED_SIZE: Final[SizeSource] = "measured"
ESTIMATED_SIZE: Final[SizeSource] = "estimated"
DEFAULT_SIZE: Final[SizeSource] = "default"


@dataclass(frozen=True, slots=True)
class GameCandidate:
    """One of a user's owned games, already scored and sized, ready for a collection strategy.

    :param size_source: Which rung of the resolution ladder produced :attr:`size_gb` -- a contributed
        ``game_measured_sizes`` row, a ``size_estimates`` band, or the flat fallback that applies when
        neither exists. Without it the three are indistinguishable in the response, and ``"default"`` is
        exactly the case that should prompt its owner for a real on-disk figure: a PS3, Vita, PSP, PS2 or
        PS1 title has no seeded band at all (``0033_seed_size_estimates`` seeds PS5 and PS4 only), so every
        one of them packs at the flat size until somebody measures one.
    """

    game_id: str
    title: str
    genre: str
    aaa_tier: str | None
    franchise: str
    composite_score: float | None
    rank_score: int
    size_gb: float
    percent_completed: int | None = None
    size_source: SizeSource = DEFAULT_SIZE
