"""Async OpenCritic (RapidAPI) client: BYOK key validation."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

OPENCRITIC_BASE_URL = "https://opencritic-api.p.rapidapi.com"

MAX_PROVIDER_DETAIL_CHARS = 300


class OpenCriticApiError(Exception):
    """Raised on a non-2xx OpenCritic response.

    :param retry_after_seconds: The response's ``Retry-After`` header, parsed to seconds-from-now, or
        ``None`` if absent or unparseable.
    :param provider_detail: A truncated, key-redacted excerpt of the response body, or ``None`` if it was
        empty or unreadable.
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
    """Return a whitespace-collapsed, truncated, key-redacted excerpt of ``response``'s body."""
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


class OpenCriticClient:
    """OpenCritic (RapidAPI) key-validation client.

    :param client: The underlying :class:`httpx.AsyncClient`.
    :param rapidapi_key: The RapidAPI key for the OpenCritic API.
    """

    def __init__(self, client: httpx.AsyncClient, rapidapi_key: str) -> None:
        self._client = client
        self._headers = {"x-rapidapi-host": "opencritic-api.p.rapidapi.com", "x-rapidapi-key": rapidapi_key}

    async def validate_key(self) -> None:
        """Confirm ``rapidapi_key`` is accepted by OpenCritic, spending one non-search request.

        :raises OpenCriticApiError: If OpenCritic rejects the key (401/403) or the request otherwise fails.
        """
        response = await self._client.get(
            f"{OPENCRITIC_BASE_URL}/game",
            params={"platforms": "ps5", "sort": "name", "order": "asc", "skip": 0},
            headers=self._headers,
        )
        self._raise_for_status(response)

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
