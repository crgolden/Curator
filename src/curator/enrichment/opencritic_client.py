"""Async OpenCritic (RapidAPI) client: zero-search-quota platform-game pagination.

Ported from ``ps_opencritic.py``'s ``urllib``-based ``api_get()``/``fetch_platform_games()``, onto
``httpx.AsyncClient``. Deliberately never calls OpenCritic's search endpoint (RapidAPI BASIC plan: 25
searches/day vs. 200 requests/day total) -- paginates ``GET /game?platforms=...`` instead, which counts
only against the larger total-requests budget. Matching lives in
:mod:`curator.enrichment.opencritic_matcher`; this module is I/O only.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, TypeVar

import httpx

from curator.enrichment.opencritic_matcher import OpenCriticGame

OPENCRITIC_BASE_URL = "https://opencritic-api.p.rapidapi.com"
DEFAULT_PAGE_SIZE = 20

# Enough of an error body to carry RapidAPI's own explanation, short enough that a wall of HTML from a
# gateway can't flood the log.
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
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        retry_after_seconds: float | None = None,
        provider_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds
        self.provider_detail = provider_detail


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
        # An instance method, not a static one, so the caller's own key can be redacted out of the body
        # excerpt before it goes anywhere.
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
            self._raise_for_status(response)
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


class OpenCriticClientProtocol(Protocol):
    """The subset of :class:`OpenCriticClient` that
    :class:`~curator.enrichment.enrichment_service.EnrichmentService` depends on -- satisfied structurally
    by both :class:`OpenCriticClient` and :class:`RotatingOpenCriticClient`."""

    async def validate_key(self) -> None: ...

    async def fetch_platform_games(
        self, platform: str, *, start_skip: int = 0, max_pages: int | None = None
    ) -> PaginationResult: ...


_T = TypeVar("_T")

_ROTATE_ON_STATUS_CODES = (401, 403, 429)


class RotatingOpenCriticClient:
    """Rotates across multiple single-key :class:`OpenCriticClient` instances behind the same interface
    :class:`~curator.enrichment.enrichment_service.EnrichmentService` depends on
    (:class:`OpenCriticClientProtocol`), advancing to the next key on a 401/403/429 from the current one --
    see :class:`curator.enrichment.rawg_client.RotatingRawgClient` for the identical rationale and
    lazy-round-robin behavior (the two are separate classes rather than one generic wrapper since
    :class:`OpenCriticClient`'s and :class:`RawgClient`'s method shapes differ).

    :param clients: Every admin OpenCritic client, one per configured key, in rotation order.
    """

    def __init__(self, clients: list[OpenCriticClientProtocol]) -> None:
        if not clients:
            raise ValueError("RotatingOpenCriticClient requires at least one client.")
        self._clients = clients
        self._index = 0

    async def validate_key(self) -> None:
        await self._call(lambda client: client.validate_key())

    async def fetch_platform_games(
        self, platform: str, *, start_skip: int = 0, max_pages: int | None = None
    ) -> PaginationResult:
        return await self._call(
            lambda client: client.fetch_platform_games(platform, start_skip=start_skip, max_pages=max_pages)
        )

    async def _call(self, invoke: Callable[[OpenCriticClientProtocol], Coroutine[Any, Any, _T]]) -> _T:
        """Try the current client, rotating to the next on a 401/403/429 and retrying the same call,
        until either one succeeds or every client has failed once (in which case the last error is
        raised, and the index stays wherever it landed rather than resetting to 0 for the next call)."""
        last_exc: OpenCriticApiError | None = None
        for _ in range(len(self._clients)):
            client = self._clients[self._index]
            try:
                return await invoke(client)
            except OpenCriticApiError as exc:
                last_exc = exc
                if exc.status_code in _ROTATE_ON_STATUS_CODES:
                    self._index = (self._index + 1) % len(self._clients)
                    continue
                raise
        assert last_exc is not None  # the loop above only exits without returning after setting last_exc
        raise last_exc
