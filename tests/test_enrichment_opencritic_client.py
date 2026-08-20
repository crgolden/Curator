"""Tests for OpenCriticClient, using httpx.MockTransport (no network, no credentials)."""

from __future__ import annotations

import httpx
import pytest

from curator.enrichment.opencritic_client import OpenCriticApiError, OpenCriticClient


class RequestRecorder:
    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)


def _client(recorder: RequestRecorder) -> OpenCriticClient:
    return OpenCriticClient(httpx.AsyncClient(transport=httpx.MockTransport(recorder)), rapidapi_key="test-key")


async def test_error_carries_the_response_body_as_provider_detail():
    body = {"message": "You are not subscribed to this API."}
    recorder = RequestRecorder([httpx.Response(403, json=body)])
    client = _client(recorder)

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.provider_detail is not None
    assert "not subscribed" in exc_info.value.provider_detail


async def test_provider_detail_redacts_the_api_key_if_the_body_echoes_it():
    recorder = RequestRecorder([httpx.Response(401, json={"message": "Invalid key test-key"})])
    client = _client(recorder)

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.provider_detail is not None
    assert "test-key" not in exc_info.value.provider_detail
    assert "[redacted]" in exc_info.value.provider_detail


async def test_validate_key_succeeds_on_200():
    recorder = RequestRecorder([httpx.Response(200, json=[])])
    client = _client(recorder)

    await client.validate_key()

    assert len(recorder.requests) == 1
    assert recorder.requests[0].url.params["platforms"] == "ps5"
    assert recorder.requests[0].headers["x-rapidapi-key"] == "test-key"


async def test_validate_key_raises_sanitized_error_on_401():
    recorder = RequestRecorder([httpx.Response(401, json={"message": "invalid key"})])
    client = _client(recorder)

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.status_code == 401
    assert "invalid key" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


async def test_parses_retry_after_seconds_header():
    recorder = RequestRecorder([httpx.Response(429, headers={"Retry-After": "60"})])
    client = _client(recorder)

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds == 60.0


async def test_retry_after_seconds_none_when_header_absent():
    recorder = RequestRecorder([httpx.Response(500)])
    client = _client(recorder)

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.retry_after_seconds is None
