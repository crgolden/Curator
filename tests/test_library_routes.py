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
    def __init__(self, run_id, kind, identity_sub, status, error=None):
        self.run_id = run_id
        self.kind = kind
        self.identity_sub = identity_sub
        self.status = status
        self.error = error


class FakeJobRunsRepository:
    def __init__(self, runs=None):
        self.runs: dict[str, FakeJobRun] = {run.run_id: run for run in (runs or [])}

    async def get(self, run_id):
        return self.runs.get(run_id)


def _build(job_runs_repository=None):
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
    assert response.json() == {"run_id": "run-1", "status": "running", "error": None}


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
