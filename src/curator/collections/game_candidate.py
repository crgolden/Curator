"""The scored, ready-to-filter/pack unit ``curator.collections`` strategies operate on.

Built by :class:`~curator.collections.collection_orchestrator.CollectionOrchestrator` from a user's
``library_entries`` + ``game_enrichment`` + a resolved install size, using
:mod:`curator.scoring.scoring_service`'s canonical composite/rank score -- the strategies themselves never
touch raw enrichment fields or call the scoring functions directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

SizeSource = Literal["measured", "download", "estimated", "capped_default", "default"]

MEASURED_SIZE: Final[SizeSource] = "measured"
DOWNLOAD_SIZE: Final[SizeSource] = "download"
ESTIMATED_SIZE: Final[SizeSource] = "estimated"
CAPPED_DEFAULT_SIZE: Final[SizeSource] = "capped_default"
DEFAULT_SIZE: Final[SizeSource] = "default"


@dataclass(frozen=True, slots=True)
class GameCandidate:
    """One of a user's owned games, already scored and sized, ready for a collection strategy.

    :param size_source: Which rung of the resolution ladder produced :attr:`size_gb`: a contributed
        ``game_measured_sizes`` row, the package size Sony's web-store entitlements reported
        (``game_download_sizes``), a ``size_estimates`` band, the platform's physical media ceiling when
        that is below the flat fallback, or the flat fallback itself. ``"default"`` is the case that should
        prompt its owner for a real on-disk figure.
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
