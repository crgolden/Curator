"""Async OpenCritic (RapidAPI) client: zero-search-quota platform-game pagination.

Ported from ``ps_opencritic.py``'s ``urllib``-based ``api_get()``/``fetch_platform_games()``, onto
``httpx.AsyncClient``. Deliberately never calls OpenCritic's search endpoint (RapidAPI BASIC plan: 25
searches/day vs. 200 requests/day total) -- paginates ``GET /game?platforms=...`` instead, which counts
only against the larger total-requests budget. Matching lives in
:mod:`curator.enrichment.opencritic_matcher`; this module is I/O only.
"""

from __future__ import annotations

from typing import Any

import httpx

from curator.enrichment.opencritic_matcher import OpenCriticGame

OPENCRITIC_BASE_URL = "https://opencritic-api.p.rapidapi.com"
DEFAULT_PAGE_SIZE = 20


def _to_game(entry: dict[str, Any]) -> OpenCriticGame:
    score = entry.get("topCriticScore")
    if score is not None and score < 0:
        score = None
    return OpenCriticGame(
        oc_game_id=entry["id"],
        name=entry["name"],
        top_critic_score=score,
        tier=entry.get("tier") or "",
        percent_recommended=entry.get("percentRecommended"),
    )


class OpenCriticClient:
    """OpenCritic (RapidAPI) platform-catalog client.

    :param client: The underlying :class:`httpx.AsyncClient`.
    :param rapidapi_key: The RapidAPI key for the OpenCritic API.
    """

    def __init__(self, client: httpx.AsyncClient, rapidapi_key: str) -> None:
        self._client = client
        self._headers = {"x-rapidapi-host": "opencritic-api.p.rapidapi.com", "x-rapidapi-key": rapidapi_key}

    async def fetch_platform_games(
        self,
        platform: str,
        *,
        start_skip: int = 0,
        max_pages: int | None = None,
    ) -> list[OpenCriticGame]:
        """Paginate every game OpenCritic has catalogued for a platform (e.g. ``"ps4"``/``"ps5"``).

        Stops when a page comes back shorter than the page size (end of catalog), when OpenCritic reports
        fewer than 10 requests remaining for the day (``X-RateLimit-Requests-Remaining`` header), or after
        ``max_pages`` pages (if given) -- whichever comes first.

        :param platform: The RapidAPI platform slug (``"ps4"`` or ``"ps5"``).
        :param start_skip: Resume pagination from this offset.
        :param max_pages: Optional page-count cap, for bounded batch runs.
        :returns: Every game fetched.
        """
        games: list[OpenCriticGame] = []
        skip = start_skip
        page_size = DEFAULT_PAGE_SIZE
        pages_fetched = 0

        while True:
            response = await self._client.get(
                f"{OPENCRITIC_BASE_URL}/game",
                params={"platforms": platform, "sort": "name", "order": "asc", "skip": skip},
                headers=self._headers,
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, list) or not data:
                break

            games.extend(_to_game(entry) for entry in data if entry.get("id") is not None and entry.get("name"))

            remaining = response.headers.get("X-RateLimit-Requests-Remaining")
            if remaining is not None and remaining.isdigit() and int(remaining) < 10:
                break

            count = len(data)
            if count < page_size:
                break

            skip += page_size
            pages_fetched += 1
            if max_pages is not None and pages_fetched >= max_pages:
                break

        return games
