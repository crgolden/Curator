"""Default console capacity by model, for ``POST /consoles`` when ``raw_capacity_gb`` is omitted.

WP3: "auto-assign a default size from the chosen console model if none is given; flag loudly, never
refuse the edit". Figures are approximate, publicly-documented *usable* storage (post-firmware/OS
overhead), not raw advertised drive size -- they exist only to seed a reasonable starting point, and
``raw_capacity_gb`` is always independently editable via ``PATCH`` afterward regardless of where it came
from.
"""

from __future__ import annotations

MODEL_CAPACITY_GB: dict[str, float] = {
    "PS5 Digital Edition": 667.0,
    "PS5 Disc Edition": 667.0,
    "PS5 Slim Digital Edition": 842.0,
    "PS5 Slim Disc Edition": 842.0,
    "PS5 Pro": 1750.0,
    "PS4 Original 500GB": 430.0,
    "PS4 Original 1TB": 850.0,
    "PS4 Slim 500GB": 430.0,
    "PS4 Slim 1TB": 850.0,
    "PS4 Pro": 850.0,
}

# Used when either no model was given at all, or one was given but isn't in MODEL_CAPACITY_GB -- a
# bigger guess than a known model's own published figure, which is why default_capacity_gb() reports it
# separately (callers flag this case more loudly than a matched-model default).
_PLATFORM_FALLBACK_GB: dict[str, float] = {"PS5": 667.0, "PS4": 430.0}
_UNKNOWN_PLATFORM_FALLBACK_GB = 500.0


def default_capacity_gb(platform: str, model: str | None) -> tuple[float, bool]:
    """Resolve a default ``raw_capacity_gb`` for a console created with no capacity given.

    :param platform: ``"PS5"``/``"PS4"`` (or anything else -- this function never raises on an
        unrecognized platform, it just falls further back; ``consoles_routes.create_console`` is what
        actually rejects an invalid platform).
    :param model: A known key of :data:`MODEL_CAPACITY_GB`, an unrecognized string, or ``None``.
    :returns: ``(capacity_gb, matched_known_model)``. The second element is ``False`` whenever the
        platform-level fallback was used instead of a specific model's own published figure, so the
        caller can flag the response more loudly in that case.
    """
    if model is not None and model in MODEL_CAPACITY_GB:
        return MODEL_CAPACITY_GB[model], True
    return _PLATFORM_FALLBACK_GB.get(platform, _UNKNOWN_PLATFORM_FALLBACK_GB), False
