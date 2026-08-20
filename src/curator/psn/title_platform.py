"""Console-platform narrowing for values stored in the database."""

from __future__ import annotations

from typing import Literal

ConsolePlatform = Literal["PS5", "PS4"]

_CONSOLE_PLATFORMS: dict[str, ConsolePlatform] = {"PS5": "PS5", "PS4": "PS4"}


def console_platform(value: str) -> ConsolePlatform:
    """Narrow a stored platform string to :data:`ConsolePlatform`, or raise :class:`ValueError`."""
    platform = _CONSOLE_PLATFORMS.get(value)
    if platform is None:
        raise ValueError(f"Unexpected console platform stored in the database: {value!r}")
    return platform
