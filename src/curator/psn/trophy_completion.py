"""Per-game trophy completion percentages: an exact lookup where possible, fuzzy name matching otherwise.

``LibraryBuildOrchestrator.match_trophies`` (ingestion time, run once per ``POST /library/refresh``)
persists each game's matched PSN trophy-title id (``library_entries.np_communication_id`` --
``0014_library_entries_trophy_match.sql``) wherever it can find one, so most reads here resolve via a cheap
``O(1)`` dict lookup by that id (:func:`_resolve_completion`). Only games with no persisted match yet
(freshly added, or one ingestion genuinely couldn't resolve) fall back to fuzzy name matching against the
same fetched trophy-titles list, the same idea :mod:`curator.enrichment.rawg_matcher` already uses to
reconcile PSN's naming against RAWG's -- reusing its ``normalize()`` directly (not ``similarity()``:
matching here needs the raw ``SequenceMatcher`` per pair so it can consult ``quick_ratio()``/
``real_quick_ratio()`` before paying for the full ``ratio()`` comparison).

Deliberately no Postgres persistence of trophy *data* here (progress/earned counts stay Redis-cached, per
``db/migrations/0001_initial.sql``'s design note) -- only the *identity* mapping is durable; see
``0014_library_entries_trophy_match.sql``'s header for that distinction.

The fuzzy fallback is O(unmatched_games x titles): for a library with a lot of not-yet-ingestion-matched
games this is a real amount of CPU (bounded by the ``quick_ratio``/``real_quick_ratio`` prefilter below, but
still non-trivial), so :func:`get_completion_map`/:func:`get_completion_result` run the whole resolution via
:func:`asyncio.to_thread` -- calling it directly from an ``async def`` would block the single-threaded event
loop for its entire duration, stalling every other request this worker is serving (including its own health
probe).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import TYPE_CHECKING

from curator.enrichment.rawg_matcher import normalize
from curator.persistence.repository import Repository
from curator.psn.errors import PsnAuthError

if TYPE_CHECKING:
    from fastapi import Request

    from curator.psn.models import TrophyTitle
    from curator.psn.trophy_client import TrophyClientFactory

DEFAULT_MATCH_THRESHOLD = 0.80

_TITLES_LIMIT = 500


def match_titles(
    titles: list[TrophyTitle],
    games: Iterable[tuple[str, str]],
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> dict[str, TrophyTitle]:
    """Match each game's title against a user's PSN trophy titles, by fuzzy name similarity.

    The matching primitive both :func:`match_completion` (read-time display, wants just the percent) and
    ``curator.library.library_build_orchestrator.LibraryBuildOrchestrator.match_trophies`` (ingestion-time
    persistence, wants the matched title's ``np_communication_id``) share, so the one-to-one assignment
    logic below exists in exactly one place.

    One-to-one: each trophy title can be claimed by at most one game. Without this, two similarly-named
    games (e.g. a base game and its "Remastered" edition) could both clear ``threshold`` against the same
    title and both display its percentage -- a wrong match, which this module treats as worse than a blank
    one. Candidate ``(game, title)`` pairs are considered best-score-first; once either side of a pair is
    claimed, neither can be claimed again.

    :param titles: The user's trophy titles (``TrophyClient.trophy_titles()``).
    :param games: ``(game_id, canonical_title)`` pairs to resolve a match for.
    :param threshold: The minimum similarity ratio to accept a match.
    :returns: ``{game_id: TrophyTitle}`` for every game that matched above ``threshold`` and has a known
        ``progress``; games with no confident (or already-claimed) match are simply absent.
    """
    games = list(games)
    named_titles = [title for title in titles if title.name and title.progress is not None]
    if not games or not named_titles:
        return {}

    normalized_games = [(game_id, normalize(canonical_title)) for game_id, canonical_title in games]
    normalized_titles = [(index, normalize(title.name or "")) for index, title in enumerate(named_titles)]

    candidates: list[tuple[float, str, int]] = []
    for game_id, game_norm in normalized_games:
        for title_index, title_norm in normalized_titles:
            matcher = SequenceMatcher(None, game_norm, title_norm)
            if matcher.real_quick_ratio() < threshold or matcher.quick_ratio() < threshold:
                continue
            score = matcher.ratio()
            if score >= threshold:
                candidates.append((score, game_id, title_index))

    candidates.sort(key=lambda candidate: candidate[0], reverse=True)
    claimed_games: set[str] = set()
    claimed_titles: set[int] = set()
    result: dict[str, TrophyTitle] = {}
    for _score, game_id, title_index in candidates:
        if game_id in claimed_games or title_index in claimed_titles:
            continue
        claimed_games.add(game_id)
        claimed_titles.add(title_index)
        result[game_id] = named_titles[title_index]
    return result


def match_completion(
    titles: list[TrophyTitle],
    games: Iterable[tuple[str, str]],
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> dict[str, int]:
    """Match each game's title against a user's PSN trophy titles, returning just the completion percent.

    A thin wrapper over :func:`match_titles` for read-time display, where the matched title's identity
    isn't needed, only its ``progress``.

    :param titles: The user's trophy titles (``TrophyClient.trophy_titles()``).
    :param games: ``(game_id, canonical_title)`` pairs to resolve a completion percentage for.
    :param threshold: The minimum similarity ratio to accept a match.
    :returns: ``{game_id: percent_completed}``; games with no confident (or already-claimed) match are
        simply absent (callers render blank).
    """
    matches = match_titles(titles, games, threshold=threshold)
    result: dict[str, int] = {}
    for game_id, title in matches.items():
        assert title.progress is not None  # match_titles only returns titles with known progress
        result[game_id] = title.progress
    return result


def _resolve_completion(titles: list[TrophyTitle], games: Iterable[tuple[str, str, str | None]]) -> dict[str, int]:
    """Resolve completion percentages for a batch of games against one fetched trophy-titles list.

    Games that already carry a persisted ``np_communication_id`` (``curator.library
    .library_build_orchestrator.LibraryBuildOrchestrator.match_trophies`` having matched them at ingestion
    time) get an exact ``O(1)`` dict lookup by that id -- no string comparison at all. Only games with no
    persisted match yet fall back to :func:`match_completion`'s fuzzy name matching against whichever
    titles the exact lookups didn't already claim -- the residual this whole module was designed to shrink
    over time as more of a user's library gets ingestion-matched.

    :param titles: The user's trophy titles (``TrophyClient.trophy_titles()``).
    :param games: ``(game_id, canonical_title, np_communication_id)`` triples.
    :returns: ``{game_id: percent_completed}``; games with no confident match are simply absent.
    """
    by_np_id: dict[str, TrophyTitle] = {
        title.np_communication_id: title for title in titles if title.np_communication_id and title.progress is not None
    }

    result: dict[str, int] = {}
    fuzzy_candidates: list[tuple[str, str]] = []
    exact_np_ids: set[str] = set()
    for game_id, canonical_title, np_communication_id in games:
        matched = by_np_id.get(np_communication_id) if np_communication_id else None
        if matched is not None:
            assert matched.progress is not None  # filtered above
            assert matched.np_communication_id is not None  # filtered above
            result[game_id] = matched.progress
            exact_np_ids.add(matched.np_communication_id)
        else:
            fuzzy_candidates.append((game_id, canonical_title))

    if fuzzy_candidates:
        remaining_titles = [title for title in titles if title.np_communication_id not in exact_np_ids]
        result.update(match_completion(remaining_titles, fuzzy_candidates))
    return result


@dataclass(frozen=True, slots=True)
class CompletionResult:
    """The outcome of one completion-matching attempt for a user's games.

    :param by_game: ``{game_id: percent_completed}``, only for games that matched confidently.
    :param available: Whether trophy data could be fetched at all for this user right now (linked,
        ``harvest_trophies`` enabled, PSN reachable) -- distinct from an individual game simply having no
        confident match. Callers that filter on completion (``curator.collections.filter_list_strategy
        .apply_filter_list``) use this to tell "nothing matched" apart from "couldn't check," so a broken
        PSN link never silently excludes every candidate from a saved collection.
    """

    by_game: dict[str, int]
    available: bool


async def _fetch_titles(request: Request, identity_sub: str) -> list[TrophyTitle] | None:
    """Fetch a user's PSN trophy titles, or ``None`` if unavailable (no link, ``harvest_trophies`` disabled,
    or PSN rejects the stored token) -- never raises."""
    repository: Repository = request.app.state.repository
    link = await repository.get_link(identity_sub)
    if link is None or not link.harvest_trophies:
        return None

    trophy_client_factory: TrophyClientFactory = request.app.state.trophy_client_factory
    try:
        client = await trophy_client_factory(identity_sub)
        return await client.trophy_titles(limit=_TITLES_LIMIT)
    except (RuntimeError, PsnAuthError):
        return None


async def get_completion_map(
    request: Request, identity_sub: str, games: Iterable[tuple[str, str, str | None]]
) -> dict[str, int]:
    """Best-effort ``{game_id: percent_completed}`` for a user's own games, never raising.

    Returns an empty map -- callers should treat that as "show blank," not an error -- when the caller has
    no PSN link, hasn't enabled ``harvest_trophies``, or PSN rejects the stored token.

    :param request: The incoming request (used to reach ``request.app.state``).
    :param identity_sub: The Curator user id (Identity's ``sub``) whose own games these are.
    :param games: ``(game_id, canonical_title, np_communication_id)`` triples -- ``np_communication_id`` is
        the value persisted by ``LibraryBuildOrchestrator.match_trophies`` (``None`` if this game hasn't
        been ingestion-matched yet), letting most games resolve via a cheap exact lookup instead of the
        fuzzy fallback.
    """
    games = list(games)
    if not games:
        return {}
    titles = await _fetch_titles(request, identity_sub)
    if titles is None:
        return {}
    return await asyncio.to_thread(_resolve_completion, titles, games)


async def get_completion_result(
    request: Request, identity_sub: str, games: Iterable[tuple[str, str, str | None]]
) -> CompletionResult:
    """Like :func:`get_completion_map`, but also reports whether trophy data was available at all for this
    user right now -- see :class:`CompletionResult`.

    :param request: The incoming request (used to reach ``request.app.state``).
    :param identity_sub: The Curator user id (Identity's ``sub``) whose own games these are.
    :param games: ``(game_id, canonical_title, np_communication_id)`` triples, as in :func:`get_completion_map`.
    """
    games = list(games)
    if not games:
        return CompletionResult(by_game={}, available=False)
    titles = await _fetch_titles(request, identity_sub)
    if titles is None:
        return CompletionResult(by_game={}, available=False)
    by_game = await asyncio.to_thread(_resolve_completion, titles, games)
    return CompletionResult(by_game=by_game, available=True)
