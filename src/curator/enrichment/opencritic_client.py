"""Async OpenCritic (RapidAPI) client: zero-search-quota platform-game pagination.

Ported from ``ps_opencritic.py``'s ``urllib``-based ``api_get()``/``fetch_platform_games()``, onto
``httpx.AsyncClient``. Deliberately never calls OpenCritic's search endpoint (RapidAPI BASIC plan: 25
searches/day vs. 200 requests/day total) -- paginates ``GET /game?platforms=...`` instead, which counts
only against the larger total-requests budget. Matching lives in
:mod:`curator.enrichment.opencritic_matcher`; this module is I/O only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from curator.enrichment.opencritic_matcher import OpenCriticGame

OPENCRITIC_BASE_URL = "https://opencritic-api.p.rapidapi.com"
DEFAULT_PAGE_SIZE = 20


class OpenCriticApiError(Exception):
    """Raised on a non-2xx OpenCritic response.

    Wrapped defensively, matching :class:`curator.enrichment.rawg_client.RawgApiError` -- the key here is
    header-based (lower leak risk than RAWG's URL query param) but some RapidAPI error response bodies
    can echo request details, so this is sanitized the same way for consistency.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class PaginationResult:
    """One :meth:`OpenCriticClient.fetch_platform_games` call's outcome.

    :param games: Every game fetched this call.
    :param next_skip: Where a subsequent call should resume (``0`` if ``exhausted``).
    :param exhausted: Whether pagination reached the end of this platform's catalog (a page came back
        shorter than the page size) -- callers should reset their cursor to ``0`` rather than getting
        permanently stuck past the end.
    """

    games: list[OpenCriticGame]
    next_skip: int
    exhausted: bool


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

    async def validate_key(self) -> None:
        """Confirm ``rapidapi_key`` is accepted by OpenCritic.

        There is no dedicated cheap validation endpoint -- every RapidAPI request counts against the
        200/day total budget regardless of endpoint. This spends exactly one request (never the
        25/day-capped search endpoint) against the non-search catalog-listing endpoint this client already
        uses everywhere else, fetching a single page for a fixed platform.

        :raises OpenCriticApiError: If OpenCritic rejects the key (401/403) or the request otherwise fails.
        """
        response = await self._client.get(
            f"{OPENCRITIC_BASE_URL}/game",
            params={"platforms": "ps5", "sort": "name", "order": "asc", "skip": 0},
            headers=self._headers,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OpenCriticApiError(
                f"OpenCritic request failed with status {exc.response.status_code}",
                status_code=exc.response.status_code,
            ) from None

    async def fetch_platform_games(
        self,
        platform: str,
        *,
        start_skip: int = 0,
        max_pages: int | None = None,
    ) -> PaginationResult:
        """Paginate OpenCritic's catalog for a platform (e.g. ``"ps4"``/``"ps5"``), resuming from
        ``start_skip``.

        Stops when a page comes back shorter than the page size (end of catalog -- ``exhausted=True``),
        when OpenCritic reports fewer than 10 requests remaining for the day
        (``X-RateLimit-Requests-Remaining`` header), or after ``max_pages`` pages (if given) -- whichever
        comes first.

        :param platform: The RapidAPI platform slug (``"ps4"`` or ``"ps5"``).
        :param start_skip: Resume pagination from this offset (see
            ``curator.enrichment.repository.EnrichmentRepository.get_opencritic_cursor``).
        :param max_pages: Optional page-count cap, so one caller's top-up can't burn through the whole
            day's budget in a single call.
        :returns: A :class:`PaginationResult`.
        :raises OpenCriticApiError: On a non-2xx response.
        """
        games: list[OpenCriticGame] = []
        skip = start_skip
        page_size = DEFAULT_PAGE_SIZE
        pages_fetched = 0
        exhausted = False

        while True:
            response = await self._client.get(
                f"{OPENCRITIC_BASE_URL}/game",
                params={"platforms": platform, "sort": "name", "order": "asc", "skip": skip},
                headers=self._headers,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise OpenCriticApiError(
                    f"OpenCritic request failed with status {exc.response.status_code}",
                    status_code=exc.response.status_code,
                ) from None
            data = response.json()
            if not isinstance(data, list) or not data:
                exhausted = True
                break

            games.extend(_to_game(entry) for entry in data if entry.get("id") is not None and entry.get("name"))

            count = len(data)
            # This page is fully processed -- advance the resume point now, before any of the break
            # conditions below, so a rate-limit-triggered stop still resumes past it next time instead of
            # re-fetching (and re-counting against quota) the exact same page.
            skip += page_size

            remaining = response.headers.get("X-RateLimit-Requests-Remaining")
            if remaining is not None and remaining.isdigit() and int(remaining) < 10:
                break

            if count < page_size:
                exhausted = True
                break

            pages_fetched += 1
            if max_pages is not None and pages_fetched >= max_pages:
                break

        return PaginationResult(games=games, next_skip=0 if exhausted else skip, exhausted=exhausted)
