"""Async RAWG API client: search + game detail fetch.

Ported from ``ps_enrich.py``'s ``urllib``-based ``rawg_get()``/``search_rawg()``/``fetch_detail()``, onto
``httpx.AsyncClient``. Matching itself (fuzzy title similarity, platform filtering) lives in
:mod:`curator.enrichment.rawg_matcher`; this module is I/O only.
"""

from __future__ import annotations

from typing import Any

import httpx

from curator.enrichment.rawg_matcher import RawgCandidate

RAWG_BASE_URL = "https://api.rawg.io/api"


def _to_candidate(result: dict[str, Any]) -> RawgCandidate:
    platform_ids = frozenset(
        platform_entry["platform"]["id"]
        for platform_entry in (result.get("platforms") or [])
        if isinstance(platform_entry.get("platform"), dict) and platform_entry["platform"].get("id") is not None
    )
    return RawgCandidate(
        rawg_game_id=result["id"],
        name=result.get("name", ""),
        platform_ids=platform_ids,
        released=result.get("released"),
    )


class RawgClient:
    """RAWG API search/detail client.

    :param client: The underlying :class:`httpx.AsyncClient`.
    :param api_key: The RAWG API key.
    """

    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        self._client = client
        self._api_key = api_key

    async def search_games(self, title: str, *, page_size: int = 5) -> list[RawgCandidate]:
        """Search RAWG for a title, returning every result as a match candidate (unfiltered).

        :param title: The title to search for.
        :param page_size: Maximum number of results to request.
        :returns: The raw search results, reduced to :class:`~curator.enrichment.rawg_matcher.RawgCandidate`.
        """
        response = await self._client.get(
            f"{RAWG_BASE_URL}/games",
            params={"key": self._api_key, "search": title, "page_size": page_size, "search_precise": "false"},
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        return [_to_candidate(result) for result in results]

    async def fetch_detail(self, rawg_game_id: int) -> dict[str, Any] | None:
        """Fetch a RAWG game's full detail record.

        :param rawg_game_id: The RAWG game id (from a search result).
        :returns: The raw detail JSON, or ``None`` if RAWG returns 404.
        """
        response = await self._client.get(f"{RAWG_BASE_URL}/games/{rawg_game_id}", params={"key": self._api_key})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        detail: dict[str, Any] = response.json()
        return detail
