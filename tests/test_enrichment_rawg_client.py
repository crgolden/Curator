"""Tests for RawgClient, using httpx.MockTransport (no network, no credentials)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http import HTTPStatus

import httpx
import pytest

from curator.enrichment.rawg_client import (
    GENRES_PATH,
    KEY_PARAM,
    MAX_PROVIDER_DETAIL_CHARS,
    PAGE_SIZE_PARAM,
    RAWG_BASE_URL,
    REDACTED_PLACEHOLDER,
    VALIDATION_PAGE_SIZE,
    RawgApiError,
    RawgClient,
)
from curator.http_headers import RETRY_AFTER_HEADER
from test_values import lowercase_token, new_opaque_token, new_positive_count, new_short_interval

API_KEY = new_opaque_token()
_CLOCK_TOLERANCE = timedelta(seconds=10)


class RequestRecorder:
    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)


def _client(recorder: RequestRecorder) -> RawgClient:
    return RawgClient(httpx.AsyncClient(transport=httpx.MockTransport(recorder)), api_key=API_KEY)


async def test_validate_key_succeeds_on_200():
    recorder = RequestRecorder([httpx.Response(HTTPStatus.OK)])
    client = _client(recorder)

    await client.validate_key()

    expected_url = httpx.URL(f"{RAWG_BASE_URL}{GENRES_PATH}")
    assert recorder.requests[0].url.host == expected_url.host
    assert recorder.requests[0].url.path == expected_url.path
    assert recorder.requests[0].url.params[KEY_PARAM] == API_KEY
    assert recorder.requests[0].url.params[PAGE_SIZE_PARAM] == str(VALIDATION_PAGE_SIZE)


def test_validation_request_uses_rawgs_published_names():
    assert GENRES_PATH == "/genres"
    assert KEY_PARAM == "key"
    assert PAGE_SIZE_PARAM == "page_size"
    assert VALIDATION_PAGE_SIZE == 1
    assert RETRY_AFTER_HEADER == "Retry-After"


async def test_validate_key_raises_sanitized_error_on_401():
    recorder = RequestRecorder([httpx.Response(HTTPStatus.UNAUTHORIZED)])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.status_code == HTTPStatus.UNAUTHORIZED
    assert API_KEY not in str(exc_info.value)


async def test_validate_key_error_never_contains_the_url_or_key():
    recorder = RequestRecorder([httpx.Response(HTTPStatus.UNAUTHORIZED, text=f"{lowercase_token()} {API_KEY}")])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    message = str(exc_info.value)
    assert API_KEY not in message
    assert httpx.URL(RAWG_BASE_URL).host not in message
    assert exc_info.value.status_code == HTTPStatus.UNAUTHORIZED
    assert exc_info.value.__cause__ is None


async def test_validate_key_throttles_via_rate_limiter():
    calls: list[None] = []

    class RecordingRateLimiter:
        async def acquire(self) -> None:
            calls.append(None)

    recorder = RequestRecorder([httpx.Response(HTTPStatus.OK)])
    client = RawgClient(
        httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        api_key=API_KEY,
        rate_limiter=RecordingRateLimiter(),
    )

    await client.validate_key()

    assert calls == [None]


async def test_error_carries_the_response_body_as_provider_detail():
    provider_explanation = lowercase_token()
    recorder = RequestRecorder([httpx.Response(HTTPStatus.UNAUTHORIZED, text=provider_explanation)])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.provider_detail == provider_explanation


async def test_provider_detail_redacts_the_api_key_if_the_body_echoes_it():
    provider_explanation = lowercase_token()
    recorder = RequestRecorder([httpx.Response(HTTPStatus.UNAUTHORIZED, text=f"{provider_explanation} {API_KEY}")])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.provider_detail == f"{provider_explanation} {REDACTED_PLACEHOLDER}"


async def test_provider_detail_is_none_for_an_empty_body():
    recorder = RequestRecorder([httpx.Response(HTTPStatus.UNAUTHORIZED)])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.provider_detail is None


async def test_provider_detail_is_truncated():
    oversized_body = lowercase_token(MAX_PROVIDER_DETAIL_CHARS + new_positive_count())
    recorder = RequestRecorder([httpx.Response(HTTPStatus.INTERNAL_SERVER_ERROR, text=oversized_body)])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.provider_detail is not None
    assert len(exc_info.value.provider_detail) == MAX_PROVIDER_DETAIL_CHARS + len("...")


async def test_parses_retry_after_seconds_header():
    retry_after_seconds = new_positive_count()
    recorder = RequestRecorder(
        [httpx.Response(HTTPStatus.TOO_MANY_REQUESTS, headers={RETRY_AFTER_HEADER: str(retry_after_seconds)})]
    )
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.status_code == HTTPStatus.TOO_MANY_REQUESTS
    assert exc_info.value.retry_after_seconds == float(retry_after_seconds)


async def test_parses_retry_after_http_date_header():
    retry_in = new_short_interval()
    retry_at = datetime.now(UTC) + retry_in
    retry_after = format_datetime(retry_at, usegmt=True)
    recorder = RequestRecorder(
        [httpx.Response(HTTPStatus.TOO_MANY_REQUESTS, headers={RETRY_AFTER_HEADER: retry_after})]
    )
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.retry_after_seconds is not None
    earliest = (retry_in - _CLOCK_TOLERANCE).total_seconds()
    assert earliest <= exc_info.value.retry_after_seconds <= retry_in.total_seconds()


async def test_retry_after_seconds_none_when_header_absent():
    recorder = RequestRecorder([httpx.Response(HTTPStatus.INTERNAL_SERVER_ERROR)])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.retry_after_seconds is None
