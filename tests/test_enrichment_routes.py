"""Tests for POST /enrichment/runs, admin-scoped, using create_app() with a fake QueuePublisher."""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings


class FakePublisher:
    def __init__(self, run_id="run-1"):
        self._run_id = run_id
        self.enrichment_calls = 0

    async def publish_enrichment_run(self):
        self.enrichment_calls += 1
        return self._run_id


def _build():
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
    return TestClient(app), validator, publisher


def test_requires_bearer_token():
    client, _validator, _publisher = _build()

    response = client.post("/enrichment/runs")

    assert response.status_code == 401


def test_non_admin_scope_is_forbidden():
    client, validator, publisher = _build()
    validator.register("token-a", _claims(is_admin=False))

    response = client.post("/enrichment/runs", headers=_bearer("token-a"))

    assert response.status_code == 403
    assert publisher.enrichment_calls == 0


def test_admin_scope_publishes_and_returns_run_id():
    client, validator, publisher = _build()
    validator.register("token-a", _claims(is_admin=True))

    response = client.post("/enrichment/runs", headers=_bearer("token-a"))

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1"}
    assert publisher.enrichment_calls == 1


def test_queue_not_configured_returns_503():
    client, validator, _publisher = _build()
    client.app.state.queue_publisher = None
    validator.register("token-a", _claims(is_admin=True))

    response = client.post("/enrichment/runs", headers=_bearer("token-a"))

    assert response.status_code == 503
