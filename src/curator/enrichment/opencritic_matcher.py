"""OpenCritic name matching: a deterministic multi-strategy cascade, ported from ``ps_opencritic.py``'s
``normalize()``/``strip_subtitle()``/``build_name_index()``/``find_match()``.

Pure -- no HTTP, no I/O; :mod:`curator.enrichment.opencritic_client` calls this against games it fetched
and cached. The cascade tries progressively looser matching strategies, in order, stopping at the first
one that finds a candidate:

1. Exact normalized-name match.
2. Subtitle-stripped name match (drop everything after the first ``": "``/`` - ``).
3. Space-stripped name match (handles "CoffeeTalk" vs. "Coffee Talk").
4. Subtitle-stripped AND space-stripped match.
5. Substring fallback A: our (possibly subtitle-stripped) title appears, word-bounded, inside a
   catalog name (e.g. "Skyrim" matching "The Elder Scrolls V: Skyrim - Special Edition").
6. Substring fallback B: a catalog name appears at the start of our title, longest catalog name wins
   (e.g. "Grand Theft Auto III" matching "Grand Theft Auto III - The Definitive Edition").
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_ROMAN_TO_ARABIC = [
    (r"\bviii\b", "8"),
    (r"\bvii\b", "7"),
    (r"\bvi\b", "6"),
    (r"\bix\b", "9"),
    (r"\biv\b", "4"),
    (r"\biii\b", "3"),
    (r"\bii\b", "2"),
]


@dataclass(frozen=True, slots=True)
class OpenCriticGame:
    """One OpenCritic "Short Game" record."""

    oc_game_id: int
    name: str
    top_critic_score: float | None
    tier: str
    percent_recommended: float | None


def normalize(title: str) -> str:
    """Aggressively normalize a title for cross-catalog matching (strips punctuation, converts roman
    numerals to arabic, collapses whitespace)."""
    normalized = unicodedata.normalize("NFKD", title)
    normalized = "".join(c for c in normalized if not unicodedata.combining(c))
    normalized = normalized.lower()
    normalized = re.sub(r"[™®©]", "", normalized)
    normalized = re.sub(r"\(tm\)|\(r\)|\(c\)", "", normalized)
    normalized = re.sub(r"[''`\"]+", "", normalized)
    normalized = re.sub(r"[–—\-]+", " ", normalized)  # noqa: RUF001 -- en/em dash, matched literally
    normalized = re.sub(r"[^a-z0-9 ]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    normalized = normalized.strip()
    for roman, arabic in _ROMAN_TO_ARABIC:
        normalized = re.sub(roman, arabic, normalized)
    return normalized


def strip_subtitle(title: str) -> str:
    """Drop everything after the first ``": "`` or `` - `` separator."""
    for separator in (": ", " - "):
        if separator in title:
            return title.split(separator)[0].strip()
    return title


def build_name_index(
    games: list[OpenCriticGame],
) -> tuple[dict[str, list[OpenCriticGame]], dict[str, list[OpenCriticGame]]]:
    """Build the two lookup indexes :func:`find_match` searches: normalized-name and space-stripped-name.

    :param games: Every cached OpenCritic game.
    :returns: ``(index, nospace_index)``.
    """
    index: dict[str, list[OpenCriticGame]] = {}
    nospace_index: dict[str, list[OpenCriticGame]] = {}

    def _add(key: str, entry: OpenCriticGame) -> None:
        index.setdefault(key, []).append(entry)
        nospace_index.setdefault(key.replace(" ", ""), []).append(entry)

    for game in games:
        key = normalize(game.name)
        _add(key, game)

        short_key = normalize(strip_subtitle(game.name))
        if short_key != key:
            _add(short_key, game)

        year_stripped = re.sub(r"\s*\(\d{4}\)\s*$", "", game.name).strip()
        if year_stripped != game.name:
            year_stripped_key = normalize(year_stripped)
            if year_stripped_key not in (key, short_key):
                _add(year_stripped_key, game)
            year_stripped_short_key = normalize(strip_subtitle(year_stripped))
            if year_stripped_short_key not in (key, short_key, year_stripped_key):
                _add(year_stripped_short_key, game)

    return index, nospace_index


def _best(candidates: list[OpenCriticGame]) -> OpenCriticGame:
    scored = [c for c in candidates if c.top_critic_score is not None]
    return max(scored, key=lambda c: c.top_critic_score or 0.0) if scored else candidates[0]


def find_match(
    title: str,
    index: dict[str, list[OpenCriticGame]],
    nospace_index: dict[str, list[OpenCriticGame]],
) -> OpenCriticGame | None:
    """Match a canonical title against the OpenCritic name indexes via the 6-strategy cascade.

    :param title: The canonical title to match.
    :param index: The normalized-name index, from :func:`build_name_index`.
    :param nospace_index: The space-stripped-name index, from :func:`build_name_index`.
    :returns: The best-matching :class:`OpenCriticGame`, or ``None``.
    """
    key = normalize(title)

    if key in index:
        return _best(index[key])

    short = normalize(strip_subtitle(title))
    if short != key and short in index:
        return _best(index[short])

    nospace_key = key.replace(" ", "")
    if nospace_key in nospace_index:
        return _best(nospace_index[nospace_key])

    short_nospace_key = short.replace(" ", "")
    if short_nospace_key != nospace_key and short_nospace_key in nospace_index:
        return _best(nospace_index[short_nospace_key])

    # Substring fallback A: our (possibly subtitle-stripped) key appears, word-bounded, inside a catalog
    # entry's key.
    search_key = short if len(short) >= len(key) - 2 else key
    if len(search_key) >= 6:
        pattern = rf"\b{re.escape(search_key)}\b"
        candidates = [entry for k, entries in index.items() if re.search(pattern, k) for entry in entries]
        if candidates:
            return _best(candidates)

    # Substring fallback B: a catalog entry's key appears at the start of our key; the longest such
    # catalog key wins (most specific match).
    best_match: OpenCriticGame | None = None
    best_len = 0
    for oc_key, entries in index.items():
        if len(oc_key) >= 8 and re.match(rf"^{re.escape(oc_key)}\b", key):
            candidate = _best(entries)
            if candidate and len(oc_key) > best_len:
                best_match = candidate
                best_len = len(oc_key)
    return best_match
