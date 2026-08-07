"""Async OpenCritic (RapidAPI) client: zero-search-quota platform-game pagination.

Ported from ``ps_opencritic.py``'s ``urllib``-based ``api_get()``/``fetch_platform_games()``, onto
``httpx.AsyncClient``. Deliberately never calls OpenCritic's search endpoint (RapidAPI BASIC plan: 25
searches/day vs. 200 requests/day total) -- paginates ``GET /game?platforms=...`` instead, which counts
only against the larger total-requests budget. Matching lives in
:mod:`curator.enrichment.opencritic_matcher`; this module is I/O only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import httpx

from curator.enrichment.opencritic_matcher import OpenCriticGame

OPENCRITIC_BASE_URL = "https://opencritic-api.p.rapidapi.com"
DEFAULT_PAGE_SIZE = 20

MAX_PROVIDER_DETAIL_CHARS = 300


class OpenCriticApiError(Exception):
    """Raised on a non-2xx OpenCritic response.

    Wrapped defensively, matching :class:`curator.enrichment.rawg_client.RawgApiError` -- the key here is
    header-based (lower leak risk than RAWG's URL query param) but some RapidAPI error response bodies
    can echo request details, so this is sanitized the same way for consistency.

    :param retry_after_seconds: The response's ``Retry-After`` header, parsed to seconds-from-now, or
        ``None`` if absent/unparseable -- RapidAPI doesn't reliably document this header either, so callers
        should fall back to a heuristic default rather than treating its absence as an error.
    :param provider_detail: A truncated, key-redacted excerpt of the response body, or ``None`` if it was
        empty or unreadable. RapidAPI answers 401/403 for an unsubscribed plan as readily as for a bad key,
        and only the body says which -- see the equivalent note on ``RawgApiError``.
    :param partial_games: Every game :meth:`OpenCriticClient.fetch_platform_games` fetched and parsed
        successfully before this error -- ``None`` unless that method set it (it never gets carried by
        constructor argument; ``_raise_for_status`` builds this exception without access to pagination
        state, so the pagination loop annotates it after catching, before re-raising).
    :param partial_next_skip: The offset of the page that failed -- resuming pagination from here re-fetches
        it, since ``skip`` only advances after a page is *successfully* parsed.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retry_after_seconds: float | None = None,
        provider_detail: str | None = None,
        partial_games: list[OpenCriticGame] | None = None,
        partial_next_skip: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.provider_detail = provider_detail
        self.partial_games = partial_games
        self.partial_next_skip = partial_next_skip


class OpenCriticNetworkError(Exception):
    """A network-level failure (timeout, connection reset) mid-pagination in
    :meth:`OpenCriticClient.fetch_platform_games` -- distinct from :class:`OpenCriticApiError` (a non-2xx
    HTTP response), but carries the same partial-progress fields so a caller can persist whatever was
    fetched before the failure instead of discarding the whole call.

    :param partial_games: Every game successfully fetched and parsed before this failure.
    :param partial_next_skip: The offset of the page that failed -- resuming from here re-fetches it,
        rather than skipping past it.
    """

    def __init__(self, *, partial_games: list[OpenCriticGame], partial_next_skip: int) -> None:
        super().__init__("Network error while paginating OpenCritic's catalog.")
        self.partial_games = partial_games
        self.partial_next_skip = partial_next_skip


def _response_detail(response: httpx.Response, api_key: str) -> str | None:
    """Return a whitespace-collapsed, truncated, key-redacted excerpt of ``response``'s body.

    Duplicated from :mod:`curator.enrichment.rawg_client` rather than shared, matching how
    ``_parse_retry_after`` is already carried independently by both provider clients.
    """
    try:
        text = response.text
    except httpx.ResponseNotRead:
        return None
    text = " ".join(text.split())
    if not text:
        return None
    if api_key:
        text = text.replace(api_key, "[redacted]")
    if len(text) > MAX_PROVIDER_DETAIL_CHARS:
        text = text[:MAX_PROVIDER_DETAIL_CHARS] + "..."
    return text


def _parse_retry_after(response: httpx.Response) -> float | None:
    """Parse a ``Retry-After`` header (RFC 7231: either delay-seconds or an HTTP-date) into seconds from
    now, or ``None`` if the header is absent or not parseable as either form.
    """
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0.0)


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
        raw=entry,
    )


class OpenCriticClient:
    """OpenCritic (RapidAPI) platform-catalog client.

    :param client: The underlying :class:`httpx.AsyncClient`.
    :param rapidapi_key: The RapidAPI key for the OpenCritic API.
    """

    def __init__(self, client: httpx.AsyncClient, rapidapi_key: str) -> None:
        self._client = client
        self._headers = {"x-rapidapi-host": "opencritic-api.p.rapidapi.com", "x-rapidapi-key": rapidapi_key}

    def _raise_for_status(self, response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise OpenCriticApiError(
                f"OpenCritic request failed with status {exc.response.status_code}",
                status_code=exc.response.status_code,
                retry_after_seconds=_parse_retry_after(exc.response),
                provider_detail=_response_detail(exc.response, self._headers["x-rapidapi-key"]),
            ) from None

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
        self._raise_for_status(response)

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
        :raises OpenCriticApiError: On a non-2xx response. Carries ``partial_games``/``partial_next_skip``
            for whatever pages completed successfully before the failure, so a caller can persist that
            progress instead of discarding the whole call.
        :raises OpenCriticNetworkError: On a network-level failure (timeout, connection reset) mid-
            pagination, carrying the same partial-progress fields.
        """
        games: list[OpenCriticGame] = []
        skip = start_skip
        page_size = DEFAULT_PAGE_SIZE
        pages_fetched = 0
        exhausted = False

        while True:
            try:
                response = await self._client.get(
                    f"{OPENCRITIC_BASE_URL}/game",
                    params={"platforms": platform, "sort": "name", "order": "asc", "skip": skip},
                    headers=self._headers,
                )
            except httpx.HTTPError as exc:
                raise OpenCriticNetworkError(partial_games=games, partial_next_skip=skip) from exc
            try:
                self._raise_for_status(response)
            except OpenCriticApiError as exc:
                exc.partial_games = games
                exc.partial_next_skip = skip
                raise
            data = response.json()
            if not isinstance(data, list) or not data:
                exhausted = True
                break

            games.extend(_to_game(entry) for entry in data if entry.get("id") is not None and entry.get("name"))

            count = len(data)
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


class OpenCriticClientProtocol(Protocol):
    """The subset of :class:`OpenCriticClient` that
    :class:`~curator.enrichment.enrichment_service.EnrichmentService` depends on. Admin-key rotation lives
    in :class:`~curator.enrichment.enrichment_service.EnrichmentService` itself (not a wrapper client
    implementing this protocol), since it needs to re-read the shared pagination cursor between key
    attempts -- see ``EnrichmentService._refresh_opencritic_platform``."""

    async def validate_key(self) -> None: ...

    async def fetch_platform_games(
        self, platform: str, *, start_skip: int = 0, max_pages: int | None = None
    ) -> PaginationResult: ...
