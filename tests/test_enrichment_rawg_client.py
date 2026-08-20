"""Tests for RawgClient, using httpx.MockTransport (no network, no credentials)."""

from __future__ import annotations

import httpx
import pytest

from curator.enrichment.rawg_client import MAX_PROVIDER_DETAIL_CHARS, RawgApiError, RawgClient


class RequestRecorder:
    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)


def _client(recorder: RequestRecorder) -> RawgClient:
    return RawgClient(httpx.AsyncClient(transport=httpx.MockTransport(recorder)), api_key="test-key")


async def test_validate_key_succeeds_on_200():
    recorder = RequestRecorder([httpx.Response(200, json={"results": []})])
    client = _client(recorder)

    await client.validate_key()

    assert recorder.requests[0].url.path.endswith("/genres")
    assert recorder.requests[0].url.params["key"] == "test-key"
    assert recorder.requests[0].url.params["page_size"] == "1"


async def test_validate_key_raises_sanitized_error_on_401():
    recorder = RequestRecorder([httpx.Response(401)])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.status_code == 401
    assert "test-key" not in str(exc_info.value)


async def test_validate_key_error_never_contains_the_url_or_key():
    recorder = RequestRecorder([httpx.Response(401, json={"error": "invalid key"})])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    message = str(exc_info.value)
    assert "test-key" not in message
    assert "api.rawg.io" not in message
    assert exc_info.value.status_code == 401
    assert exc_info.value.__cause__ is None


async def test_validate_key_throttles_via_rate_limiter():
    calls: list[None] = []

    class RecordingRateLimiter:
        async def acquire(self) -> None:
            calls.append(None)

    recorder = RequestRecorder([httpx.Response(200, json={"results": []})])
    client = RawgClient(
        httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
        api_key="test-key",
        rate_limiter=RecordingRateLimiter(),
    )

    await client.validate_key()

    assert len(calls) == 1


async def test_error_carries_the_response_body_as_provider_detail():
    recorder = RequestRecorder([httpx.Response(401, json={"error": "The monthly limit has been reached"})])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.provider_detail is not None
    assert "The monthly limit has been reached" in exc_info.value.provider_detail


async def test_provider_detail_redacts_the_api_key_if_the_body_echoes_it():
    recorder = RequestRecorder([httpx.Response(401, json={"error": "Invalid key: test-key"})])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.provider_detail is not None
    assert "test-key" not in exc_info.value.provider_detail
    assert "[redacted]" in exc_info.value.provider_detail


async def test_provider_detail_is_none_for_an_empty_body():
    recorder = RequestRecorder([httpx.Response(401)])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.provider_detail is None


async def test_provider_detail_is_truncated():
    recorder = RequestRecorder([httpx.Response(500, text="x" * (MAX_PROVIDER_DETAIL_CHARS * 3))])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.provider_detail is not None
    assert len(exc_info.value.provider_detail) == MAX_PROVIDER_DETAIL_CHARS + len("...")


async def test_parses_retry_after_seconds_header():
    recorder = RequestRecorder([httpx.Response(429, headers={"Retry-After": "120"})])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds == 120.0


async def test_parses_retry_after_http_date_header():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    retry_at = datetime.now(timezone.utc) + timedelta(seconds=300)
    recorder = RequestRecorder([httpx.Response(429, headers={"Retry-After": format_datetime(retry_at, usegmt=True)})])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.retry_after_seconds is not None
    assert 290 <= exc_info.value.retry_after_seconds <= 300


async def test_retry_after_seconds_none_when_header_absent():
    recorder = RequestRecorder([httpx.Response(500)])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.retry_after_seconds is None
