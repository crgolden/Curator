"""The closed ``games.content_kind`` vocabulary (``0061_games_content_kind.sql``).

``None`` is "not classified" and is browsed as a game; only a proven non-game is excluded from browsing.
"""

from __future__ import annotations

from typing import Final, Literal

ContentKind = Literal["game", "media_app", "add_on", "demo", "soundtrack", "theme", "subscription"]

CONTENT_KINDS: Final[tuple[ContentKind, ...]] = (
    "game",
    "media_app",
    "add_on",
    "demo",
    "soundtrack",
    "theme",
    "subscription",
)

GAME_KIND: Final[ContentKind] = "game"

EVERY_KIND: Final = "all"
"""The ``kind`` query value that lifts the browsing exclusion entirely."""

BROWSABLE_KIND_SQL: Final = "(g.content_kind IS NULL OR g.content_kind = 'game')"
"""The default browsing predicate over an aliased ``games g``: unclassified rows browse as games."""
