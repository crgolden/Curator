"""Async RAWG API client: search + game detail fetch.

Ported from ``ps_enrich.py``'s ``urllib``-based ``rawg_get()``/``search_rawg()``/``fetch_detail()``, onto
``httpx.AsyncClient``. Matching itself (fuzzy title similarity, platform filtering) lives in
:mod:`curator.enrichment.rawg_matcher`; this module is I/O only.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, TypeVar

import httpx

from curator.enrichment.rawg_matcher import RawgCandidate
from curator.psn.session import NullRateLimiter, RateLimiter

RAWG_BASE_URL = "https://api.rawg.io/api"

# Enough of an error body to carry RAWG's own sentence-long explanation, short enough that a wall of
# HTML from a proxy can't flood the log.
MAX_PROVIDER_DETAIL_CHARS = 300


class RawgApiError(Exception):
    """Raised on a non-2xx RAWG response.

    The message is always safe to persist/log/display -- it never includes the request URL or query
    string, which carries the caller's API key (``?key=...``). Callers must always re-raise this ``from
    None`` (not ``from exc``) so a downstream ``logger.exception(...)`` doesn't still render the
    original ``httpx.HTTPStatusError``'s message (which does embed the URL) via the exception chain.

    :param status_code: The RAWG response's HTTP status code, for callers branching on auth failure
        (401/403) vs. transient (429/5xx).
    :param retry_after_seconds: The response's ``Retry-After`` header, parsed to seconds-from-now, or
        ``None`` if the header was absent or unparseable -- RAWG doesn't reliably document this header for
        quota exhaustion (it's a monthly quota, not a sliding window), so callers should fall back to a
        heuristic default rather than treating its absence as an error.
    :param provider_detail: A truncated, key-redacted excerpt of the response body, or ``None`` if it was
        empty or unreadable. RAWG answers 401 for several unrelated conditions -- a wrong key, an
        unverified account, an exhausted monthly quota -- and only the body distinguishes them. Without it
        the caller can do no better than guess, and telling a user to re-check a key that is in fact
        correct sends them somewhere the problem isn't.
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

    The redaction is defensive rather than a response to a known leak: RAWG takes the key as a query
    parameter, and error payloads that echo the request back are common enough across APIs not to bet
    against. Returns ``None`` for an empty or unread body so callers can say "<no response body>" rather
    than log an empty string.
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

    def _raise_for_status(self, response: httpx.Response) -> None:
        # An instance method, not a static one, so the caller's own key can be redacted out of the body
        # excerpt before it goes anywhere.
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RawgApiError(
                f"RAWG request failed with status {exc.response.status_code}",
                status_code=exc.response.status_code,
                retry_after_seconds=_parse_retry_after(exc.response),
                provider_detail=_response_detail(exc.response, self._api_key),
            ) from None


class RawgClientProtocol(Protocol):
    """The subset of :class:`RawgClient` that :class:`~curator.enrichment.enrichment_service.EnrichmentService`
    depends on -- satisfied structurally by both :class:`RawgClient` and :class:`RotatingRawgClient`."""

    async def search_games(self, title: str, *, page_size: int = 5) -> list[RawgCandidate]: ...

    async def validate_key(self) -> None: ...

    async def fetch_detail(self, rawg_game_id: int) -> dict[str, Any] | None: ...


_T = TypeVar("_T")

_ROTATE_ON_STATUS_CODES = (401, 403, 429)


class RotatingRawgClient:
    """Rotates across multiple single-key :class:`RawgClient` instances behind the same interface
    :class:`~curator.enrichment.enrichment_service.EnrichmentService` depends on
    (:class:`RawgClientProtocol`), advancing to the next key on a 401/403/429 from the current one.

    Used only for the admin catalog-wide singleton (``curator.app``) when more than one ``RawgApiKey`` is
    configured -- per-user BYOK is always exactly one key, so it never needs this. Transparent to
    everything above it: a :class:`RawgApiError` only reaches ``EnrichmentService``'s existing 401/403/429
    translation once every configured key has already been tried and failed, so that translation logic
    never needs to know rotation happened.

    :param clients: Every admin RAWG client, one per configured key, in rotation order.
    """

    def __init__(self, clients: list[RawgClientProtocol]) -> None:
        if not clients:
            raise ValueError("RotatingRawgClient requires at least one client.")
        self._clients = clients
        self._index = 0

    async def search_games(self, title: str, *, page_size: int = 5) -> list[RawgCandidate]:
        return await self._call(lambda client: client.search_games(title, page_size=page_size))

    async def validate_key(self) -> None:
        await self._call(lambda client: client.validate_key())

    async def fetch_detail(self, rawg_game_id: int) -> dict[str, Any] | None:
        return await self._call(lambda client: client.fetch_detail(rawg_game_id))

    async def _call(self, invoke: Callable[[RawgClientProtocol], Coroutine[Any, Any, _T]]) -> _T:
        """Try the current client, rotating to the next on a 401/403/429 and retrying the same call,
        until either one succeeds or every client has failed once (in which case the last error is
        raised, and the index stays wherever it landed rather than resetting to 0 for the next call)."""
        last_exc: RawgApiError | None = None
        for _ in range(len(self._clients)):
            client = self._clients[self._index]
            try:
                return await invoke(client)
            except RawgApiError as exc:
                last_exc = exc
                if exc.status_code in _ROTATE_ON_STATUS_CODES:
                    self._index = (self._index + 1) % len(self._clients)
                    continue
                raise
        assert last_exc is not None  # the loop above only exits without returning after setting last_exc
        raise last_exc
