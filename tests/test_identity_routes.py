"""Tests for GET /identity -- create_app wired with a hand-written fake identity_client_factory, mirroring
test_trophy_routes.py's style.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from curator.psn.account_client import Account
from curator.psn.errors import PsnAuthError
from test_routes import EMAIL, SUB, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _seed_link


class FakeAccountClient:
    """Stands in for AccountClient: canned whoami() result, or raises PsnAuthError when armed."""

    def __init__(self, *, raise_auth_error=False):
        self.raise_auth_error = raise_auth_error

    async def whoami(self):
        if self.raise_auth_error:
            raise PsnAuthError("boom")
        return Account(account_id="psn-account-1", online_id="TestOnlineId", region="United States")


class FakeAccountClientFactory:
    """Records every ``sub`` requested; raises ``RuntimeError`` for any ``sub`` not explicitly linked."""

    def __init__(self):
        self.linked: dict[str, FakeAccountClient] = {}
        self.calls: list[str] = []

    async def __call__(self, sub):
        self.calls.append(sub)
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(f"No PSN link for user {sub!r}; cannot fetch identity.")
        return client


def _build(identity_client_factory=None, repository=None):
    settings = _make_settings()
    repository = repository if repository is not None else FakeRepository()
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(sub=SUB, email=EMAIL))
    app = create_app(
        settings,
        repository=repository,
        token_validator=validator,
        identity_client_factory=identity_client_factory or FakeAccountClientFactory(),
    )
    return TestClient(app), app.state.identity_client_factory


def _build_linked(identity_client_factory=None):
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB, harvest_identity=True)
    return _build(identity_client_factory, repository=repository)


def test_get_identity_no_link_is_404():
    client, _ = _build()
    response = client.get("/identity", headers=_bearer("valid-token"))
    assert response.status_code == 404


def test_get_identity_harvest_identity_disabled_is_403():
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB, harvest_identity=False)
    client, _ = _build(repository=repository)

    response = client.get("/identity", headers=_bearer("valid-token"))
    assert response.status_code == 403


def test_get_identity_happy_path():
    factory = FakeAccountClientFactory()
    factory.linked[SUB] = FakeAccountClient()
    client, _ = _build_linked(factory)

    response = client.get("/identity", headers=_bearer("valid-token"))

    assert response.status_code == 200
    assert response.json() == {
        "account_id": "psn-account-1",
        "online_id": "TestOnlineId",
        "region": "United States",
    }
    assert factory.calls == [SUB]


def test_get_identity_psn_auth_error_is_401():
    factory = FakeAccountClientFactory()
    factory.linked[SUB] = FakeAccountClient(raise_auth_error=True)
    client, _ = _build_linked(factory)

    response = client.get("/identity", headers=_bearer("valid-token"))
    assert response.status_code == 401
