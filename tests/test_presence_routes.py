"""Tests for GET /presence -- create_app wired with a hand-written fake presence_client_factory, mirroring
test_trophy_routes.py's style.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from curator.psn.errors import PsnAuthError
from curator.psn.models import Presence
from test_routes import EMAIL, SUB, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _seed_link


class FakePresenceClient:
    """Stands in for PresenceClient: canned presence() result, or raises PsnAuthError when armed."""

    def __init__(self, *, raise_auth_error=False):
        self.raise_auth_error = raise_auth_error

    async def presence(self, online_id=None, account_id=None):
        if self.raise_auth_error:
            raise PsnAuthError("boom")
        return Presence(online_status="online", platform="PS5", last_online_date=None, game_title="Game A")


class FakePresenceClientFactory:
    """Records every ``sub`` requested; raises ``RuntimeError`` for any ``sub`` not explicitly linked."""

    def __init__(self):
        self.linked: dict[str, FakePresenceClient] = {}
        self.calls: list[str] = []

    async def __call__(self, sub):
        self.calls.append(sub)
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(f"No PSN link for user {sub!r}; cannot fetch presence.")
        return client


def _build(presence_client_factory=None, repository=None):
    settings = _make_settings()
    repository = repository if repository is not None else FakeRepository()
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(sub=SUB, email=EMAIL))
    app = create_app(
        settings,
        repository=repository,
        token_validator=validator,
        presence_client_factory=presence_client_factory or FakePresenceClientFactory(),
    )
    return TestClient(app), app.state.presence_client_factory


def _build_linked(presence_client_factory=None):
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB, harvest_presence=True)
    return _build(presence_client_factory, repository=repository)


def test_get_presence_no_link_is_404():
    client, _ = _build()
    response = client.get("/presence", headers=_bearer("valid-token"))
    assert response.status_code == 404


def test_get_presence_harvest_presence_disabled_is_403():
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB, harvest_presence=False)
    client, _ = _build(repository=repository)

    response = client.get("/presence", headers=_bearer("valid-token"))
    assert response.status_code == 403


def test_get_presence_happy_path():
    factory = FakePresenceClientFactory()
    factory.linked[SUB] = FakePresenceClient()
    client, _ = _build_linked(factory)

    response = client.get("/presence", headers=_bearer("valid-token"))

    assert response.status_code == 200
    assert response.json() == {
        "online_status": "online",
        "platform": "PS5",
        "last_online_date": None,
        "game_title": "Game A",
    }
    assert factory.calls == [SUB]


def test_get_presence_psn_auth_error_is_401():
    factory = FakePresenceClientFactory()
    factory.linked[SUB] = FakePresenceClient(raise_auth_error=True)
    client, _ = _build_linked(factory)

    response = client.get("/presence", headers=_bearer("valid-token"))
    assert response.status_code == 401
