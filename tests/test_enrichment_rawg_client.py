"""Tests for RawgClient, using httpx.MockTransport (no network, no credentials)."""

from __future__ import annotations

import httpx
import pytest

from curator.enrichment.rawg_client import MAX_PROVIDER_DETAIL_CHARS, RawgApiError, RawgClient, RotatingRawgClient
from curator.enrichment.rawg_matcher import RawgCandidate


class RequestRecorder:
    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)


def _client(recorder: RequestRecorder) -> RawgClient:
    return RawgClient(httpx.AsyncClient(transport=httpx.MockTransport(recorder)), api_key="test-key")


async def test_search_games_maps_platforms_to_candidates():
    body = {
        "results": [
            {
                "id": 123,
                "name": "God of War",
                "platforms": [{"platform": {"id": 18}}, {"platform": {"id": 187}}],
                "released": "2018-04-20",
            }
        ]
    }
    recorder = RequestRecorder([httpx.Response(200, json=body)])
    client = _client(recorder)

    candidates = await client.search_games("God of War")

    assert len(candidates) == 1
    assert candidates[0].rawg_game_id == 123
    assert candidates[0].name == "God of War"
    assert candidates[0].platform_ids == frozenset({18, 187})
    assert candidates[0].released == "2018-04-20"
    assert recorder.requests[0].url.params["search"] == "God of War"
    assert recorder.requests[0].url.params["key"] == "test-key"


async def test_search_games_empty_results():
    recorder = RequestRecorder([httpx.Response(200, json={"results": []})])
    client = _client(recorder)

    assert await client.search_games("Nonexistent Game") == []


async def test_search_games_missing_results_key():
    recorder = RequestRecorder([httpx.Response(200, json={})])
    client = _client(recorder)

    assert await client.search_games("Anything") == []


async def test_fetch_detail_returns_json():
    recorder = RequestRecorder([httpx.Response(200, json={"id": 123, "name": "God of War"})])
    client = _client(recorder)

    detail = await client.fetch_detail(123)

    assert detail == {"id": 123, "name": "God of War"}


async def test_fetch_detail_returns_none_on_404():
    recorder = RequestRecorder([httpx.Response(404)])
    client = _client(recorder)

    assert await client.fetch_detail(999) is None


async def test_fetch_detail_raises_sanitized_error_on_server_error():
    recorder = RequestRecorder([httpx.Response(500)])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.fetch_detail(1)

    assert exc_info.value.status_code == 500
    assert exc_info.value.__cause__ is None


async def test_search_games_raises_sanitized_error_never_containing_the_url_or_key():
    recorder = RequestRecorder([httpx.Response(401, json={"error": "invalid key"})])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.search_games("Some Title")

    message = str(exc_info.value)
    assert "test-key" not in message
    assert "api.rawg.io" not in message
    assert exc_info.value.status_code == 401
    assert exc_info.value.__cause__ is None  # 'from None' -- logger.exception must not render the original URL


async def test_validate_key_succeeds_on_200():
    recorder = RequestRecorder([httpx.Response(200, json={"results": []})])
    client = _client(recorder)

    await client.validate_key()  # no exception

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


async def test_fetch_detail_parses_retry_after_seconds_header():
    recorder = RequestRecorder([httpx.Response(429, headers={"Retry-After": "120"})])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.fetch_detail(1)

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds == 120.0


async def test_fetch_detail_parses_retry_after_http_date_header():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime

    retry_at = datetime.now(timezone.utc) + timedelta(seconds=300)
    recorder = RequestRecorder([httpx.Response(429, headers={"Retry-After": format_datetime(retry_at, usegmt=True)})])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.fetch_detail(1)

    assert exc_info.value.retry_after_seconds is not None
    assert 290 <= exc_info.value.retry_after_seconds <= 300


async def test_fetch_detail_retry_after_seconds_none_when_header_absent():
    recorder = RequestRecorder([httpx.Response(500)])
    client = _client(recorder)

    with pytest.raises(RawgApiError) as exc_info:
        await client.fetch_detail(1)

    assert exc_info.value.retry_after_seconds is None


async def test_search_games_throttles_via_rate_limiter():
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

    await client.search_games("Anything")

    assert len(calls) == 1


class FakeSingleKeyRawgClient:
    """A hand-written fake satisfying RawgClientProtocol -- used instead of the real RawgClient so
    RotatingRawgClient's rotation logic is tested in isolation from HTTP/mock-transport concerns."""

    def __init__(self, *, key_label: str, raises: RawgApiError | None = None):
        self.key_label = key_label
        self._raises = raises
        self.search_calls: list[str] = []
        self.detail_calls: list[int] = []
        self.validate_calls = 0

    async def search_games(self, title, *, page_size=5):
        self.search_calls.append(title)
        if self._raises is not None:
            raise self._raises
        return [RawgCandidate(rawg_game_id=0, name=self.key_label, platform_ids=frozenset())]

    async def validate_key(self):
        self.validate_calls += 1
        if self._raises is not None:
            raise self._raises

    async def fetch_detail(self, rawg_game_id):
        self.detail_calls.append(rawg_game_id)
        if self._raises is not None:
            raise self._raises
        return {"id": rawg_game_id}


def test_rotating_client_requires_at_least_one_client():
    with pytest.raises(ValueError, match="at least one client"):
        RotatingRawgClient([])


async def test_rotating_client_rotates_to_next_key_on_401_and_succeeds():
    failing = FakeSingleKeyRawgClient(key_label="key-1", raises=RawgApiError("bad key", status_code=401))
    working = FakeSingleKeyRawgClient(key_label="key-2")
    client = RotatingRawgClient([failing, working])

    result = await client.search_games("Some Title")

    assert [candidate.name for candidate in result] == ["key-2"]
    assert failing.search_calls == ["Some Title"]
    assert working.search_calls == ["Some Title"]


@pytest.mark.parametrize("status_code", [401, 403, 429])
async def test_rotating_client_rotates_on_every_rotatable_status_code(status_code):
    failing = FakeSingleKeyRawgClient(key_label="key-1", raises=RawgApiError("x", status_code=status_code))
    working = FakeSingleKeyRawgClient(key_label="key-2")
    client = RotatingRawgClient([failing, working])

    result = await client.search_games("Title")

    assert [candidate.name for candidate in result] == ["key-2"]


async def test_rotating_client_raises_last_error_once_every_key_exhausted():
    first = FakeSingleKeyRawgClient(key_label="key-1", raises=RawgApiError("first", status_code=429))
    second = FakeSingleKeyRawgClient(key_label="key-2", raises=RawgApiError("second", status_code=429))
    client = RotatingRawgClient([first, second])

    with pytest.raises(RawgApiError) as exc_info:
        await client.search_games("Title")

    assert str(exc_info.value) == "second"
    assert first.search_calls == ["Title"]
    assert second.search_calls == ["Title"]


async def test_rotating_client_does_not_rotate_on_a_non_rotatable_status_code():
    failing = FakeSingleKeyRawgClient(key_label="key-1", raises=RawgApiError("server error", status_code=500))
    working = FakeSingleKeyRawgClient(key_label="key-2")
    client = RotatingRawgClient([failing, working])

    with pytest.raises(RawgApiError):
        await client.search_games("Title")

    assert working.search_calls == []  # never rotated to -- a 500 isn't a rotate-on status code


async def test_rotating_client_stays_on_last_good_index_across_calls():
    failing = FakeSingleKeyRawgClient(key_label="key-1", raises=RawgApiError("bad key", status_code=401))
    working = FakeSingleKeyRawgClient(key_label="key-2")
    client = RotatingRawgClient([failing, working])

    await client.search_games("First Call")
    await client.search_games("Second Call")


    assert failing.search_calls == ["First Call"]
    assert working.search_calls == ["First Call", "Second Call"]


async def test_rotating_client_delegates_fetch_detail_and_validate_key():
    working = FakeSingleKeyRawgClient(key_label="key-1")
    client = RotatingRawgClient([working])

    detail = await client.fetch_detail(42)
    await client.validate_key()

    assert detail == {"id": 42}
    assert working.detail_calls == [42]
    assert working.validate_calls == 1
