"""Async RAWG API client: BYOK key validation."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, TypeVar

import httpx

from curator.psn.session import NullRateLimiter, RateLimiter

RAWG_BASE_URL = "https://api.rawg.io/api"

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


class RawgClient:
    """RAWG API key-validation client.

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

    async def validate_key(self) -> None:
        """Confirm ``api_key`` is accepted by RAWG, without spending any real search/detail quota.

        Calls the cheapest possible endpoint (``/genres`` with ``page_size=1``) -- RAWG documents no rate
        limit for this API, so a single extra request per key-save has no meaningful cost.

        :raises RawgApiError: If RAWG rejects the key (401/403) or the request otherwise fails.
        """
        response = await self._get(f"{RAWG_BASE_URL}/genres", params={"key": self._api_key, "page_size": 1})
        self._raise_for_status(response)

    async def _get(self, url: str, *, params: dict[str, Any]) -> httpx.Response:
        await self._rate_limiter.acquire()
        return await self._client.get(url, params=params)

    def _raise_for_status(self, response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RawgApiError(
                f"RAWG request failed with status {exc.response.status_code}",
                status_code=exc.response.status_code,
                retry_after_seconds=_parse_retry_after(exc.response),
                provider_detail=_response_detail(exc.response, self._api_key),
            ) from None


_T = TypeVar("_T")

_ROTATE_ON_STATUS_CODES = (401, 403, 429)
