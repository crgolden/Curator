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
            "NPIA",
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
    {"SUBC", "SCEA", "NPUP", "NPEP", "NPJP", "NPUK", "NPEK", "NPXS", "PSNP"}
)


def platform_for_title_id(title_id: str | None) -> str | None:
    """Resolve a PSN title id to a ``platforms.platform_id``.

    :param title_id: A PSN title id such as ``BLUS30233_00``; may be ``None``.
    :returns: The platform id, or ``None`` when the title id is absent, belongs to a non-title
        entitlement (subscriptions, promotions, streaming apps), or uses an unrecognised prefix.
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
    :returns: ``True`` for subscription, promotion and streaming-app entitlements.
    """
    return (title_id or "")[:4].upper() in _NON_TITLE_PREFIXES
