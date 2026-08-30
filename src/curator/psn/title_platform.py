"""The PlayStation platform vocabulary, and how a PSN title id maps onto it.

:data:`CONSOLE_PLATFORM_IDS` mirrors the ``platforms`` reference table
(``0032_platforms.sql``) in ``sort_order``. ``tests/test_schema.py`` asserts the two agree against a real
database, so a platform added by a migration and not here fails CI rather than surfacing as a ``400`` on a
console the schema already accepts.

:data:`_PLATFORM_BY_TITLE_ID_PREFIX` and :data:`_NON_TITLE_PREFIXES` mirror
``Functions/Functions/Curator/Psn/TitlePlatform.cs``. Ingestion runs in that repo, so the two must agree;
see ``AGENTS/Curator.md`` on Curator-owned vocabularies read by the Functions worker.
"""

from __future__ import annotations

from typing import Final, Literal

ConsolePlatform = Literal["PS5", "PS4", "PS3", "PSVITA", "PSP", "PS2", "PS1"]

CONSOLE_PLATFORM_IDS: Final[tuple[ConsolePlatform, ...]] = ("PS5", "PS4", "PS3", "PSVITA", "PSP", "PS2", "PS1")

_CONSOLE_PLATFORMS: Final[dict[str, ConsolePlatform]] = {platform: platform for platform in CONSOLE_PLATFORM_IDS}

_PLATFORM_BY_TITLE_ID_PREFIX: Final[dict[str, ConsolePlatform]] = {
    "PPSA": "PS5",
    "CUSA": "PS4",
    "BLUS": "PS3",
    "BLES": "PS3",
    "BLJM": "PS3",
    "BLJS": "PS3",
    "BCUS": "PS3",
    "BCES": "PS3",
    "BCJS": "PS3",
    "BCAS": "PS3",
    "NPUB": "PS3",
    "NPEB": "PS3",
    "NPJB": "PS3",
    "NPHB": "PS3",
    "NPUA": "PS3",
    "NPEA": "PS3",
    "NPJA": "PS3",
    "NPHA": "PS3",
    "NPUO": "PS3",
    "NPEO": "PS3",
    "NPUX": "PS3",
    "NPEX": "PS3",
    "PCSA": "PSVITA",
    "PCSB": "PSVITA",
    "PCSC": "PSVITA",
    "PCSD": "PSVITA",
    "PCSE": "PSVITA",
    "PCSF": "PSVITA",
    "PCSG": "PSVITA",
    "PCSH": "PSVITA",
    "VLUS": "PSVITA",
    "VCUS": "PSVITA",
    "VCJS": "PSVITA",
    "VCAS": "PSVITA",
    "NPVA": "PSVITA",
    "NPVB": "PSVITA",
    "NPVC": "PSVITA",
    "NPVX": "PSVITA",
    "UCUS": "PSP",
    "UCES": "PSP",
    "UCJS": "PSP",
    "UCAS": "PSP",
    "ULUS": "PSP",
    "ULES": "PSP",
    "ULJM": "PSP",
    "ULJS": "PSP",
    "NPUG": "PSP",
    "NPEG": "PSP",
    "NPJG": "PSP",
    "NPHG": "PSP",
    "NPUZ": "PSP",
    "NPEZ": "PSP",
    "NPJZ": "PSP",
}

_NON_TITLE_PREFIXES: Final[frozenset[str]] = frozenset(
    {"SUBC", "SCEA", "NPIA", "NPUP", "NPEP", "NPJP", "NPUK", "NPEK", "NPXS", "PSNP"}
)

_PREFIX_LENGTH: Final = 4


def console_platform(value: str) -> ConsolePlatform:
    """Narrow a stored platform string to :data:`ConsolePlatform`, or raise :class:`ValueError`."""
    platform = _CONSOLE_PLATFORMS.get(value)
    if platform is None:
        raise ValueError(f"Unexpected console platform stored in the database: {value!r}")
    return platform


def platform_vocabulary_message() -> str:
    """The accepted-platform list, for a ``400`` body -- built from :data:`CONSOLE_PLATFORM_IDS` so a
    widened vocabulary can never leave the rejection message naming a narrower one."""
    return "platform must be one of " + ", ".join(f'"{platform}"' for platform in CONSOLE_PLATFORM_IDS) + "."


def platform_for_title_id(title_id: str | None) -> ConsolePlatform | None:
    """The platform a PSN title id's prefix names, or ``None``.

    ``None`` covers three genuinely different cases the caller must not conflate: no title id, a
    :func:`is_non_title_entitlement` prefix, and a prefix nothing in the map claims. All three mean "do not
    assert a platform for this entitlement", which is why they share a return value.
    """
    prefix = _prefix(title_id)
    if prefix is None or prefix in _NON_TITLE_PREFIXES:
        return None
    return _PLATFORM_BY_TITLE_ID_PREFIX.get(prefix)


def is_non_title_entitlement(title_id: str | None) -> bool:
    """Whether a title id names a PS Plus SKU, a reward child, or a media app rather than a game."""
    prefix = _prefix(title_id)
    return prefix is not None and prefix in _NON_TITLE_PREFIXES


def _prefix(title_id: str | None) -> str | None:
    if title_id is None or not title_id.strip():
        return None
    return title_id[:_PREFIX_LENGTH].upper()
