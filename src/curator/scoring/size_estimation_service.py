"""Install-size estimation.

Per-title overrides and the AAA/AA/Indie x genre-class heuristic bands both live in the ``size_estimates``
table as config-as-data rows (:class:`SizeEstimate`), so recalibrating the formula does not mean editing
code. Publisher-tier classification is not this module's job -- callers resolve ``aaa_tier`` first and pass
it in.
"""

from __future__ import annotations

from dataclasses import dataclass

from curator.psn.title_platform import ConsolePlatform


@dataclass(frozen=True, slots=True)
class SizeEstimate:
    """One row from ``size_estimates``.

    Exactly one of ``title_pattern`` or ``aaa_tier`` is set on every real row (enforced by the table's own
    CHECK constraint) -- a title-pattern override, or a generic tier/genre-class band.
    """

    estimate_id: str
    title_pattern: str | None
    aaa_tier: str | None
    genre_class: str | None
    platform: ConsolePlatform
    size_gb: float


def estimate_install_size_gb(
    title: str,
    genre: str,
    platform: ConsolePlatform,
    aaa_tier: str,
    estimates: list[SizeEstimate],
) -> float | None:
    """Estimate a title's install size in GB.

    Resolution order: (1) the longest matching per-title substring override for the game's platform, else
    (2) the most specific matching AAA/AA/Indie x genre-class band for that platform, else (3) that tier's
    generic (no genre-class) band for that platform. Returns ``None`` if nothing matches at all -- there
    is deliberately no catch-all fallback size: an unestimatable title is a real gap the ``size_estimates``
    table should be extended to cover, not something to silently paper over here. ``0033_seed_size_estimates``
    seeds PS5 and PS4 only, so every other platform resolves to ``None`` today and the caller's own
    fallback applies.

    :param title: The game's canonical title.
    :param genre: The game's resolved genre, as stored in ``game_enrichment``.
    :param platform: Which platform's edition to estimate.
    :param aaa_tier: The game's publisher tier (``"AAA"``/``"AA"``/``"Indie"``), already resolved by the caller.
    :param estimates: Every row from ``size_estimates``.
    :returns: The estimated size in GB, or ``None`` if no row matches.
    """
    title_lower = title.lower()

    title_matches = [
        estimate
        for estimate in estimates
        if estimate.title_pattern and estimate.platform == platform and estimate.title_pattern.lower() in title_lower
    ]
    if title_matches:
        best_title_match = max(title_matches, key=lambda estimate: len(estimate.title_pattern or ""))
        return float(best_title_match.size_gb)

    genre_lower = (genre or "").lower()
    tier_matches = [
        estimate for estimate in estimates if estimate.aaa_tier == aaa_tier and estimate.platform == platform
    ]

    genre_matches = [
        estimate for estimate in tier_matches if estimate.genre_class and estimate.genre_class.lower() in genre_lower
    ]
    if genre_matches:
        best_genre_match = max(genre_matches, key=lambda estimate: len(estimate.genre_class or ""))
        return float(best_genre_match.size_gb)

    generic_matches = [estimate for estimate in tier_matches if not estimate.genre_class]
    if generic_matches:
        return float(generic_matches[0].size_gb)

    return None
