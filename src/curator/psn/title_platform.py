"""Platform classification for PSN title ids."""

from __future__ import annotations

_TITLE_ID_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("PS5", ("PPSA",)),
    ("PS4", ("CUSA",)),
    (
        "PS3",
        (
            "BLUS",
            "BLES",
            "BLJM",
            "BLJS",
            "BCUS",
            "BCES",
            "BCJS",
            "BCAS",
            "NPUB",
            "NPEB",
            "NPJB",
            "NPHB",
            "NPUA",
            "NPEA",
            "NPJA",
            "NPHA",
            "NPUO",
            "NPEO",
            "NPUX",
            "NPEX",
        ),
    ),
    (
        "PSVITA",
        (
            "PCSA",
            "PCSB",
            "PCSC",
            "PCSD",
            "PCSE",
            "PCSF",
            "PCSG",
            "PCSH",
            "VLUS",
            "VCUS",
            "VCJS",
            "VCAS",
            "NPVA",
            "NPVB",
            "NPVC",
            "NPVX",
        ),
    ),
    (
        "PSP",
        (
            "UCUS",
            "UCES",
            "UCJS",
            "UCAS",
            "ULUS",
            "ULES",
            "ULJM",
            "ULJS",
            "NPUG",
            "NPEG",
            "NPJG",
            "NPHG",
            "NPUZ",
            "NPEZ",
            "NPJZ",
        ),
    ),
)

_NON_TITLE_PREFIXES: frozenset[str] = frozenset(
    {"SUBC", "SCEA", "NPIA", "NPUP", "NPEP", "NPJP", "NPUK", "NPEK", "NPXS", "PSNP"}
)

_PLATFORM_ID_ALIASES: dict[str, str] = {
    "ps5": "PS5",
    "ps4": "PS4",
    "ps3": "PS3",
    "psvita": "PSVITA",
    "psp": "PSP",
}


def platform_for_title_id(title_id: str | None) -> str | None:
    """Resolve a PSN title id to a ``platforms.platform_id``.

    :param title_id: A PSN title id such as ``BLUS30233_00``; may be ``None``.
    :returns: The platform id, or ``None`` when the title id is absent, belongs to a non-title
        entitlement (subscriptions, promotions, streaming apps, system data), or uses an unrecognised
        prefix.
    """
    prefix = (title_id or "")[:4].upper()
    if not prefix or prefix in _NON_TITLE_PREFIXES:
        return None

    for platform, prefixes in _TITLE_ID_PREFIXES:
        if prefix in prefixes:
            return platform
    return None


def is_non_title_entitlement(title_id: str | None) -> bool:
    """Report whether a title id belongs to something other than a game.

    :param title_id: A PSN title id; may be ``None``.
    :returns: ``True`` for subscription, promotion, streaming-app and system-data entitlements.
    """
    return (title_id or "")[:4].upper() in _NON_TITLE_PREFIXES


def normalize_platform_id(platform_id: str | None) -> str | None:
    """Normalise a raw ``entitlementAttributes[].platformId`` value to a ``platforms.platform_id``.

    :param platform_id: PSN's lowercase platform id (e.g. ``"ps4"``), or ``None``.
    :returns: The uppercase platform id, or ``None`` for an absent, unrecognised, or non-console value
        (e.g. ``"xperia"``, a phone entitlement rather than a platform).
    """
    return _PLATFORM_ID_ALIASES.get((platform_id or "").lower())
