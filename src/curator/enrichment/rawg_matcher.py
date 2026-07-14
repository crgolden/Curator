"""RAWG fuzzy title matching, ported from ``ps_enrich.py``'s ``normalize()``/``similarity()``/the matching
half of ``search_rawg()``. Pure -- no HTTP, no I/O; :mod:`curator.enrichment.rawg_client` calls this
against results it fetched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

# PS4/PS5 platform ids on RAWG. Rejecting results without either prevents matching PC-only, mobile, or
# prior-gen PlayStation titles (PS3/PS2/PS1) that share a name with a PS4/PS5 game -- RAWG's broader
# "playstation" parent-platform slug covers PS1-PS5 and is too imprecise for this.
PS4_PLATFORM_ID = 18
PS5_PLATFORM_ID = 187

# Below this SequenceMatcher ratio, a "best" candidate isn't trusted as a real match.
DEFAULT_MATCH_THRESHOLD = 0.45


@dataclass(frozen=True, slots=True)
class RawgCandidate:
    """One RAWG search result, reduced to what matching needs."""

    rawg_game_id: int
    name: str
    platform_ids: frozenset[int]
    released: str | None = None


def normalize(title: str) -> str:
    """Normalize a title for fuzzy comparison: strip TM/reg/copyright symbols, collapse whitespace."""
    normalized = re.sub(r"[™®©]", "", title)
    normalized = re.sub(r"[''`]", "'", normalized)
    normalized = re.sub(r"[–—]", "-", normalized)  # noqa: RUF001 -- en/em dash, matched literally
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip().lower()


def similarity(a: str, b: str) -> float:
    """Return a ``0.0``-``1.0`` similarity ratio between two titles (order-independent)."""
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


def find_best_match(
    title: str,
    candidates: list[RawgCandidate],
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> RawgCandidate | None:
    """Pick the best-matching PS4/PS5 RAWG candidate for a title, or ``None`` if nothing clears the bar.

    :param title: The canonical title to match against.
    :param candidates: The RAWG search results to consider.
    :param threshold: The minimum similarity ratio to accept a match; below this, ``None`` is returned
        even if a candidate was the closest of the bunch.
    :returns: The best-matching :class:`RawgCandidate`, or ``None``.
    """
    best: RawgCandidate | None = None
    best_score = 0.0
    for candidate in candidates:
        if not candidate.platform_ids & {PS4_PLATFORM_ID, PS5_PLATFORM_ID}:
            continue
        score = similarity(title, candidate.name)
        if score > best_score:
            best_score = score
            best = candidate

    if best is None or best_score < threshold:
        return None
    return best
