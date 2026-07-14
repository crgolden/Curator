"""Shared genre-tag prioritization, ported near-verbatim from ``Tools\\PlayStation\\ps_genre.py``.

Both RAWG and the PS Store return an unordered/API-defined list of genre tags per game (e.g. RAWG returns
``["Simulation", "Sports", "Racing"]`` for F1 23; PS Store returns ``["Family", "Simulation"]`` for
Overcooked! All You Can Eat). Neither source orders tags by "how most people would describe this game" --
naively taking ``tags[0]`` mislabels sports/racing sims as "Simulation" (RAWG tags nearly every
sports/racing sim this way, inconsistently -- e.g. FIFA 22 in the same RAWG cache is tagged just
``["Sports"]``, no Simulation).

The priority ranking itself now lives in the ``genres`` table (this is the gap the migration plan's
"would it make sense to have a genres table?" question closed) instead of a hardcoded module-level list --
:func:`pick_genre_subgenre` takes the resolved ``name -> priority`` mapping as a parameter.
"""

from __future__ import annotations


def pick_genre_subgenre(tags: list[str], priorities: dict[str, int]) -> tuple[str, str]:
    """Return ``(genre, subgenre)`` picked from ``tags`` by specificity priority.

    Ties, and tags absent from ``priorities``, keep their original relative order (Python's sort is
    stable) and rank below every listed tag.

    :param tags: The raw, unordered genre tags for one game.
    :param priorities: ``name.lower() -> priority`` (lower number = more specific/preferred), from the
        ``genres`` table.
    :returns: ``(genre, subgenre)``, or ``("", "")`` if ``tags`` is empty.
    """
    if not tags:
        return "", ""
    fallback_rank = (max(priorities.values()) + 1) if priorities else 0
    ranked = sorted(tags, key=lambda tag: priorities.get(tag.lower(), fallback_rank))
    genre = ranked[0]
    subgenre = ranked[1] if len(ranked) > 1 else ""
    return genre, subgenre
