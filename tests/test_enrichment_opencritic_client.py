"""Tests for OpenCriticClient, using httpx.MockTransport (no network, no credentials)."""

from __future__ import annotations

import httpx
import pytest

from curator.enrichment.opencritic_client import (
    PLATFORMS_PARAM,
    RAPIDAPI_HOST_HEADER,
    RAPIDAPI_KEY_HEADER,
    REDACTED_PLACEHOLDER,
    VALIDATION_PLATFORM,
    OpenCriticApiError,
    OpenCriticClient,
)
from curator.http_headers import RETRY_AFTER_HEADER
from test_values import lowercase_token, new_opaque_token, new_positive_count

API_KEY = new_opaque_token()


class RequestRecorder:
    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)


def _client(recorder: RequestRecorder) -> OpenCriticClient:
    return OpenCriticClient(httpx.AsyncClient(transport=httpx.MockTransport(recorder)), rapidapi_key=API_KEY)


async def test_error_carries_the_response_body_as_provider_detail():
    provider_explanation = lowercase_token()
    recorder = RequestRecorder([httpx.Response(403, text=provider_explanation)])
    client = _client(recorder)

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.provider_detail == provider_explanation


async def test_provider_detail_redacts_the_api_key_if_the_body_echoes_it():
    provider_explanation = lowercase_token()
    recorder = RequestRecorder([httpx.Response(401, text=f"{provider_explanation} {API_KEY}")])
    client = _client(recorder)

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.provider_detail == f"{provider_explanation} {REDACTED_PLACEHOLDER}"


async def test_validate_key_succeeds_on_200():
    recorder = RequestRecorder([httpx.Response(200, json=[])])
    client = _client(recorder)

    await client.validate_key()

    assert len(recorder.requests) == 1
    assert recorder.requests[0].url.params[PLATFORMS_PARAM] == VALIDATION_PLATFORM
    assert recorder.requests[0].headers[RAPIDAPI_KEY_HEADER] == API_KEY


def test_validation_request_uses_opencritics_published_names():
    assert PLATFORMS_PARAM == "platforms"
    assert VALIDATION_PLATFORM == "ps5"
    assert RAPIDAPI_KEY_HEADER == "x-rapidapi-key"
    assert RAPIDAPI_HOST_HEADER == "x-rapidapi-host"
    assert RETRY_AFTER_HEADER == "Retry-After"


async def test_validate_key_raises_sanitized_error_on_401():
    provider_explanation = lowercase_token()
    recorder = RequestRecorder([httpx.Response(401, text=provider_explanation)])
    client = _client(recorder)

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.status_code == 401
    assert provider_explanation not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


async def test_parses_retry_after_seconds_header():
    retry_after_seconds = new_positive_count()
    recorder = RequestRecorder([httpx.Response(429, headers={RETRY_AFTER_HEADER: str(retry_after_seconds)})])
    client = _client(recorder)

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds == float(retry_after_seconds)


async def test_retry_after_seconds_none_when_header_absent():
    recorder = RequestRecorder([httpx.Response(500)])
    client = _client(recorder)

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.validate_key()

    assert exc_info.value.retry_after_seconds is None
