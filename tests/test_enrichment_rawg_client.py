"""Tests for RawgClient, using httpx.MockTransport (no network, no credentials)."""

from __future__ import annotations

import httpx
import pytest

from curator.enrichment.rawg_client import RawgClient


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


async def test_fetch_detail_raises_on_server_error():
    recorder = RequestRecorder([httpx.Response(500)])
    client = _client(recorder)

    with pytest.raises(httpx.HTTPStatusError):
        await client.fetch_detail(1)
