"""Tests for OpenCriticClient, using httpx.MockTransport (no network, no credentials)."""

from __future__ import annotations

import httpx

from curator.enrichment.opencritic_client import OpenCriticClient


class RequestRecorder:
    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)


def _client(recorder: RequestRecorder) -> OpenCriticClient:
    return OpenCriticClient(httpx.AsyncClient(transport=httpx.MockTransport(recorder)), rapidapi_key="test-key")


async def test_fetch_platform_games_stops_on_short_page():
    page = [{"id": 1, "name": "Game A", "topCriticScore": 85, "tier": "Strong", "percentRecommended": 90}]
    recorder = RequestRecorder([httpx.Response(200, json=page)])
    client = _client(recorder)

    games = await client.fetch_platform_games("ps5")

    assert len(games) == 1
    assert games[0].oc_game_id == 1
    assert games[0].name == "Game A"
    assert recorder.requests[0].headers["x-rapidapi-key"] == "test-key"


async def test_fetch_platform_games_negative_score_becomes_none():
    page = [{"id": 1, "name": "Unscored Game", "topCriticScore": -1, "tier": "", "percentRecommended": None}]
    recorder = RequestRecorder([httpx.Response(200, json=page)])
    client = _client(recorder)

    games = await client.fetch_platform_games("ps5")

    assert games[0].top_critic_score is None


async def test_fetch_platform_games_paginates_full_pages():
    full_page = [
        {"id": i, "name": f"Game {i}", "topCriticScore": 70, "tier": "Fair", "percentRecommended": 50}
        for i in range(20)
    ]
    short_page = [{"id": 100, "name": "Last Game", "topCriticScore": 70, "tier": "Fair", "percentRecommended": 50}]
    recorder = RequestRecorder([httpx.Response(200, json=full_page), httpx.Response(200, json=short_page)])
    client = _client(recorder)

    games = await client.fetch_platform_games("ps4")

    assert len(games) == 21
    assert recorder.requests[0].url.params["skip"] == "0"
    assert recorder.requests[1].url.params["skip"] == "20"


async def test_fetch_platform_games_stops_when_rate_limit_low():
    page = [{"id": 1, "name": "Game A", "topCriticScore": 80, "tier": "Strong", "percentRecommended": 80}] * 20
    recorder = RequestRecorder([httpx.Response(200, json=page, headers={"X-RateLimit-Requests-Remaining": "5"})])
    client = _client(recorder)

    await client.fetch_platform_games("ps5")

    assert len(recorder.requests) == 1


async def test_fetch_platform_games_empty_response_stops_immediately():
    recorder = RequestRecorder([httpx.Response(200, json=[])])
    client = _client(recorder)

    assert await client.fetch_platform_games("ps5") == []


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

    await client.fetch_platform_games("ps5", max_pages=1)

    assert len(recorder.requests) == 1  # first full page fetched, then max_pages=1 stops before a second
