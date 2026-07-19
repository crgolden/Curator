"""Async RAWG API client: search + game detail fetch.

Ported from ``ps_enrich.py``'s ``urllib``-based ``rawg_get()``/``search_rawg()``/``fetch_detail()``, onto
``httpx.AsyncClient``. Matching itself (fuzzy title similarity, platform filtering) lives in
:mod:`curator.enrichment.rawg_matcher`; this module is I/O only.
"""

from __future__ import annotations

from typing import Any

import httpx

from curator.enrichment.rawg_matcher import RawgCandidate
from curator.psn.session import NullRateLimiter, RateLimiter

RAWG_BASE_URL = "https://api.rawg.io/api"


class RawgApiError(Exception):
    """Raised on a non-2xx RAWG response.

    The message is always safe to persist/log/display -- it never includes the request URL or query
    string, which carries the caller's API key (``?key=...``). Callers must always re-raise this ``from
    None`` (not ``from exc``) so a downstream ``logger.exception(...)`` doesn't still render the
    original ``httpx.HTTPStatusError``'s message (which does embed the URL) via the exception chain.

    :param status_code: The RAWG response's HTTP status code, for callers branching on auth failure
        (401/403) vs. transient (429/5xx).
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


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
    :param rate_limiter: Throttles outbound requests; defaults to no throttling. Production supplies a
        per-user :class:`~curator.psn.rate_limiter.RedisRateLimiter` (see ``curator.app``) since this is
        typically a user's own, likely-free-tier key.
    """

    def __init__(self, client: httpx.AsyncClient, api_key: str, *, rate_limiter: RateLimiter | None = None) -> None:
        self._client = client
        self._api_key = api_key
        self._rate_limiter = rate_limiter or NullRateLimiter()

    async def search_games(self, title: str, *, page_size: int = 5) -> list[RawgCandidate]:
        """Search RAWG for a title, returning every result as a match candidate (unfiltered).

        :param title: The title to search for.
        :param page_size: Maximum number of results to request.
        :returns: The raw search results, reduced to :class:`~curator.enrichment.rawg_matcher.RawgCandidate`.
        :raises RawgApiError: On a non-2xx response.
        """
        response = await self._get(
            f"{RAWG_BASE_URL}/games",
            params={"key": self._api_key, "search": title, "page_size": page_size, "search_precise": "false"},
        )
        self._raise_for_status(response)
        results = response.json().get("results") or []
        return [_to_candidate(result) for result in results]

    async def validate_key(self) -> None:
        """Confirm ``api_key`` is accepted by RAWG, without spending any real search/detail quota.

        Calls the cheapest possible endpoint (``/genres`` with ``page_size=1``) -- RAWG documents no rate
        limit for this API, so a single extra request per key-save has no meaningful cost.

        :raises RawgApiError: If RAWG rejects the key (401/403) or the request otherwise fails.
        """
        response = await self._get(f"{RAWG_BASE_URL}/genres", params={"key": self._api_key, "page_size": 1})
        self._raise_for_status(response)

    async def fetch_detail(self, rawg_game_id: int) -> dict[str, Any] | None:
        """Fetch a RAWG game's full detail record.

        :param rawg_game_id: The RAWG game id (from a search result).
        :returns: The raw detail JSON, or ``None`` if RAWG returns 404.
        :raises RawgApiError: On a non-2xx, non-404 response.
        """
        response = await self._get(f"{RAWG_BASE_URL}/games/{rawg_game_id}", params={"key": self._api_key})
        if response.status_code == 404:
            return None
        self._raise_for_status(response)
        detail: dict[str, Any] = response.json()
        return detail

    async def _get(self, url: str, *, params: dict[str, Any]) -> httpx.Response:
        await self._rate_limiter.acquire()
        return await self._client.get(url, params=params)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RawgApiError(
                f"RAWG request failed with status {exc.response.status_code}", status_code=exc.response.status_code
            ) from None
