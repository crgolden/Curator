"""Tests for POST /library/refresh, using create_app() with a fake QueuePublisher."""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings


class FakePublisher:
    def __init__(self, run_id="run-1"):
        self._run_id = run_id
        self.library_refresh_calls = []

    async def publish_library_refresh(self, identity_sub):
        self.library_refresh_calls.append(identity_sub)
        return self._run_id


class FakeJobRun:
    def __init__(self, run_id, kind, identity_sub, status, error=None, result_summary=None):
        self.run_id = run_id
        self.kind = kind
        self.identity_sub = identity_sub
        self.status = status
        self.error = error
        self.result_summary = result_summary


class FakeJobRunsRepository:
    def __init__(self, runs=None):
        self.runs: dict[str, FakeJobRun] = {run.run_id: run for run in (runs or [])}

    async def get(self, run_id):
        return self.runs.get(run_id)


class FakeLibraryGameView:
    def __init__(self, game_id, title, rawg_enriched=False, opencritic_enriched=False):
        self.game_id = game_id
        self.title = title
        self.rawg_enriched = rawg_enriched
        self.opencritic_enriched = opencritic_enriched


class FakeLibraryRepository:
    def __init__(self, games_by_sub=None):
        self._games_by_sub = games_by_sub or {}

    async def list_entries_with_enrichment(self, identity_sub):
        return self._games_by_sub.get(identity_sub, [])


def _build(job_runs_repository=None, library_repository=None):
    repository = FakeRepository()
    token_crypto = TokenCrypto(Fernet.generate_key())
    validator = FakeTokenValidator()
    publisher = FakePublisher()
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=validator,
    )
    app.state.queue_publisher = publisher
    app.state.job_runs_repository = job_runs_repository or FakeJobRunsRepository()
    app.state.library_repository = library_repository or FakeLibraryRepository()
    return TestClient(app), validator, publisher


def test_requires_bearer_token():
    client, _validator, _publisher = _build()

    response = client.post("/library/refresh")

    assert response.status_code == 401


def test_publishes_for_the_callers_own_sub_and_returns_run_id():
    client, validator, publisher = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/library/refresh", headers=_bearer("token-a"))

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1"}
    assert publisher.library_refresh_calls == ["sub-a"]


def test_queue_not_configured_returns_503():
    client, validator, _publisher = _build()
    client.app.state.queue_publisher = None
    validator.register("token-a", _claims())

    response = client.post("/library/refresh", headers=_bearer("token-a"))

    assert response.status_code == 503


def test_get_status_returns_run_for_owner():
    run = FakeJobRun("run-1", "library_refresh", "sub-a", "running")
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library/refresh/run-1", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == {"run_id": "run-1", "status": "running", "error": None, "result_summary": None}


def test_get_status_returns_result_summary_when_present():
    summary = {"rawg_enriched_titles": ["Elden Ring"], "opencritic_topup_incomplete": False}
    run = FakeJobRun("run-1", "library_refresh", "sub-a", "succeeded", result_summary=summary)
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library/refresh/run-1", headers=_bearer("token-a"))

    assert response.json()["result_summary"] == summary


def test_get_status_unknown_run_returns_404():
    client, validator, _publisher = _build()
    validator.register("token-a", _claims())

    response = client.get("/library/refresh/unknown", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_get_status_not_owned_returns_404():
    run = FakeJobRun("run-1", "library_refresh", "sub-b", "succeeded")
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library/refresh/run-1", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_get_status_enrichment_run_returns_404():
    run = FakeJobRun("run-1", "enrichment", None, "succeeded")
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims())

    response = client.get("/library/refresh/run-1", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_get_library_requires_bearer_token():
    client, _validator, _publisher = _build()

    assert client.get("/library").status_code == 401


def test_get_library_returns_callers_own_games_with_enrichment_status():
    games = [
        FakeLibraryGameView("game-1", "Elden Ring", rawg_enriched=True, opencritic_enriched=True),
        FakeLibraryGameView("game-2", "Unmatched Game", rawg_enriched=False, opencritic_enriched=False),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == [
        {"game_id": "game-1", "title": "Elden Ring", "rawg_enriched": True, "opencritic_enriched": True},
        {"game_id": "game-2", "title": "Unmatched Game", "rawg_enriched": False, "opencritic_enriched": False},
    ]


def test_get_library_returns_empty_list_for_a_user_with_no_entries():
    client, validator, _publisher = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == []


def test_get_library_scoped_to_caller_only():
    games_a = [FakeLibraryGameView("game-1", "A's Game")]
    games_b = [FakeLibraryGameView("game-2", "B's Game")]
    client, validator, _publisher = _build(
        library_repository=FakeLibraryRepository({"sub-a": games_a, "sub-b": games_b})
    )
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library", headers=_bearer("token-a"))

    assert [game["title"] for game in response.json()] == ["A's Game"]
