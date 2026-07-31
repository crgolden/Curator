"""Tests for OpenCriticClient, using httpx.MockTransport (no network, no credentials)."""

from __future__ import annotations

import httpx
import pytest

from curator.enrichment.opencritic_client import (
    OpenCriticApiError,
    OpenCriticClient,
    PaginationResult,
    RotatingOpenCriticClient,
)
from curator.enrichment.opencritic_matcher import OpenCriticGame


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
    # RapidAPI answers 403 for an unsubscribed plan as readily as for a bad key; only the body says which.
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


async def test_fetch_platform_games_stops_on_short_page():
    page = [{"id": 1, "name": "Game A", "topCriticScore": 85, "tier": "Strong", "percentRecommended": 90}]
    recorder = RequestRecorder([httpx.Response(200, json=page)])
    client = _client(recorder)

    result = await client.fetch_platform_games("ps5")

    assert len(result.games) == 1
    assert result.games[0].oc_game_id == 1
    assert result.games[0].name == "Game A"
    assert result.exhausted is True
    assert result.next_skip == 0
    assert recorder.requests[0].headers["x-rapidapi-key"] == "test-key"


async def test_fetch_platform_games_negative_score_becomes_none():
    page = [{"id": 1, "name": "Unscored Game", "topCriticScore": -1, "tier": "", "percentRecommended": None}]
    recorder = RequestRecorder([httpx.Response(200, json=page)])
    client = _client(recorder)

    result = await client.fetch_platform_games("ps5")

    assert result.games[0].top_critic_score is None


async def test_fetch_platform_games_paginates_full_pages():
    full_page = [
        {"id": i, "name": f"Game {i}", "topCriticScore": 70, "tier": "Fair", "percentRecommended": 50}
        for i in range(20)
    ]
    short_page = [{"id": 100, "name": "Last Game", "topCriticScore": 70, "tier": "Fair", "percentRecommended": 50}]
    recorder = RequestRecorder([httpx.Response(200, json=full_page), httpx.Response(200, json=short_page)])
    client = _client(recorder)

    result = await client.fetch_platform_games("ps4")

    assert len(result.games) == 21
    assert result.exhausted is True
    assert recorder.requests[0].url.params["skip"] == "0"
    assert recorder.requests[1].url.params["skip"] == "20"


async def test_fetch_platform_games_stops_when_rate_limit_low():
    page = [{"id": 1, "name": "Game A", "topCriticScore": 80, "tier": "Strong", "percentRecommended": 80}] * 20
    recorder = RequestRecorder([httpx.Response(200, json=page, headers={"X-RateLimit-Requests-Remaining": "5"})])
    client = _client(recorder)

    result = await client.fetch_platform_games("ps5")

    assert len(recorder.requests) == 1
    assert result.exhausted is False
    assert result.next_skip == 20


async def test_fetch_platform_games_empty_response_stops_immediately():
    recorder = RequestRecorder([httpx.Response(200, json=[])])
    client = _client(recorder)

    result = await client.fetch_platform_games("ps5")

    assert result.games == []
    assert result.exhausted is True
    assert result.next_skip == 0


async def test_fetch_platform_games_respects_start_skip():
    recorder = RequestRecorder([httpx.Response(200, json=[])])
    client = _client(recorder)

    await client.fetch_platform_games("ps5", start_skip=3800)

    assert recorder.requests[0].url.params["skip"] == "3800"


async def test_fetch_platform_games_respects_max_pages():
    full_page = [
        {"id": i, "name": f"Game {i}", "topCriticScore": 70, "tier": "Fair", "percentRecommended": 50}
        for i in range(20)
    ]
    recorder = RequestRecorder([httpx.Response(200, json=full_page), httpx.Response(200, json=full_page)])
    client = _client(recorder)

    result = await client.fetch_platform_games("ps5", max_pages=1)

    assert len(recorder.requests) == 1  # first full page fetched, then max_pages=1 stops before a second
    assert result.exhausted is False
    assert result.next_skip == 20


async def test_validate_key_succeeds_on_200():
    recorder = RequestRecorder([httpx.Response(200, json=[])])
    client = _client(recorder)

    await client.validate_key()  # no exception

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


async def test_fetch_platform_games_raises_sanitized_error_on_non_2xx():
    recorder = RequestRecorder([httpx.Response(401, json={"message": "invalid key"})])
    client = _client(recorder)

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.fetch_platform_games("ps5")

    assert exc_info.value.status_code == 401
    assert "invalid key" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


async def test_fetch_platform_games_parses_retry_after_seconds_header():
    recorder = RequestRecorder([httpx.Response(429, headers={"Retry-After": "60"})])
    client = _client(recorder)

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.fetch_platform_games("ps5")

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds == 60.0


async def test_fetch_platform_games_retry_after_seconds_none_when_header_absent():
    recorder = RequestRecorder([httpx.Response(500)])
    client = _client(recorder)

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.fetch_platform_games("ps5")

    assert exc_info.value.retry_after_seconds is None


class FakeSingleKeyOpenCriticClient:
    """A hand-written fake satisfying OpenCriticClientProtocol -- used instead of the real
    OpenCriticClient so RotatingOpenCriticClient's rotation logic is tested in isolation from
    HTTP/mock-transport concerns."""

    def __init__(self, *, key_label: str, raises: OpenCriticApiError | None = None):
        self.key_label = key_label
        self._raises = raises
        self.validate_calls = 0
        self.fetch_calls: list[str] = []

    async def validate_key(self):
        self.validate_calls += 1
        if self._raises is not None:
            raise self._raises

    async def fetch_platform_games(self, platform, *, start_skip=0, max_pages=None):
        self.fetch_calls.append(platform)
        if self._raises is not None:
            raise self._raises
        game = OpenCriticGame(
            oc_game_id=0, name=self.key_label, top_critic_score=None, tier="", percent_recommended=None
        )
        return PaginationResult(games=[game], next_skip=0, exhausted=True)


def test_rotating_client_requires_at_least_one_client():
    with pytest.raises(ValueError, match="at least one client"):
        RotatingOpenCriticClient([])


async def test_rotating_client_rotates_to_next_key_on_401_and_succeeds():
    failing = FakeSingleKeyOpenCriticClient(key_label="key-1", raises=OpenCriticApiError("bad key", status_code=401))
    working = FakeSingleKeyOpenCriticClient(key_label="key-2")
    client = RotatingOpenCriticClient([failing, working])

    result = await client.fetch_platform_games("ps5")

    assert result.games[0].name == "key-2"
    assert failing.fetch_calls == ["ps5"]
    assert working.fetch_calls == ["ps5"]


@pytest.mark.parametrize("status_code", [401, 403, 429])
async def test_rotating_client_rotates_on_every_rotatable_status_code(status_code):
    failing = FakeSingleKeyOpenCriticClient(key_label="key-1", raises=OpenCriticApiError("x", status_code=status_code))
    working = FakeSingleKeyOpenCriticClient(key_label="key-2")
    client = RotatingOpenCriticClient([failing, working])

    result = await client.fetch_platform_games("ps5")

    assert result.games[0].name == "key-2"


async def test_rotating_client_raises_last_error_once_every_key_exhausted():
    first = FakeSingleKeyOpenCriticClient(key_label="key-1", raises=OpenCriticApiError("first", status_code=429))
    second = FakeSingleKeyOpenCriticClient(key_label="key-2", raises=OpenCriticApiError("second", status_code=429))
    client = RotatingOpenCriticClient([first, second])

    with pytest.raises(OpenCriticApiError) as exc_info:
        await client.fetch_platform_games("ps5")

    assert str(exc_info.value) == "second"
    assert first.fetch_calls == ["ps5"]
    assert second.fetch_calls == ["ps5"]


async def test_rotating_client_does_not_rotate_on_a_non_rotatable_status_code():
    error = OpenCriticApiError("server error", status_code=500)
    failing = FakeSingleKeyOpenCriticClient(key_label="key-1", raises=error)
    working = FakeSingleKeyOpenCriticClient(key_label="key-2")
    client = RotatingOpenCriticClient([failing, working])

    with pytest.raises(OpenCriticApiError):
        await client.fetch_platform_games("ps5")

    assert working.fetch_calls == []  # never rotated to -- a 500 isn't a rotate-on status code


async def test_rotating_client_stays_on_last_good_index_across_calls():
    failing = FakeSingleKeyOpenCriticClient(key_label="key-1", raises=OpenCriticApiError("bad key", status_code=401))
    working = FakeSingleKeyOpenCriticClient(key_label="key-2")
    client = RotatingOpenCriticClient([failing, working])

    await client.fetch_platform_games("ps5")
    await client.fetch_platform_games("ps4")

    # the second call goes straight to key-2 rather than re-trying key-1 first
    assert failing.fetch_calls == ["ps5"]
    assert working.fetch_calls == ["ps5", "ps4"]


async def test_rotating_client_delegates_validate_key():
    working = FakeSingleKeyOpenCriticClient(key_label="key-1")
    client = RotatingOpenCriticClient([working])

    await client.validate_key()

    assert working.validate_calls == 1
