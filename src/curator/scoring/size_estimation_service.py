"""Install-size estimation, ported from ``Tools\\PlayStation\\ps_sizes.py``'s ``get_install_size()``.

The legacy script's hardcoded ``KNOWN_SIZES`` per-title override dict and its empirically-tuned
AAA/AA/Indie x genre-class heuristic bands both now live in the ``size_estimates`` table as config-as-data
rows (:class:`SizeEstimate`) instead of Python literals, closing the same "recalibrating the formula means
editing code" gap the migration fixed for genres/publisher tiers. Publisher-tier classification itself is
:mod:`curator.enrichment.publisher_tier`'s job, not this module's -- callers resolve ``aaa_tier`` first and
pass it in.
"""

from __future__ import annotations

from dataclasses import dataclass


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
    platform: str  # "PS5" | "PS4"
    size_gb: float


def estimate_install_size_gb(
    title: str,
    genre: str,
    is_ps5: bool,
    aaa_tier: str,
    estimates: list[SizeEstimate],
) -> float | None:
    """Estimate a title's install size in GB.

    Resolution order: (1) the longest matching per-title substring override for the game's platform, else
    (2) the most specific matching AAA/AA/Indie x genre-class band for that platform, else (3) that tier's
    generic (no genre-class) band for that platform. Returns ``None`` if nothing matches at all -- unlike
    the legacy script's hardcoded final "return 16" fallback, an unestimatable title is a real gap the
    ``size_estimates`` table should be extended to cover, not something to silently paper over here.

    :param title: The game's canonical title.
    :param genre: The game's resolved genre (from :func:`~curator.scoring.genre_service.pick_genre_subgenre`).
    :param is_ps5: Whether to estimate for the PS5 edition (``False`` estimates the PS4 edition).
    :param aaa_tier: The game's publisher tier (``"AAA"``/``"AA"``/``"Indie"``), already resolved by the caller.
    :param estimates: Every row from ``size_estimates``.
    :returns: The estimated size in GB, or ``None`` if no row matches.
    """
    platform = "PS5" if is_ps5 else "PS4"
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
