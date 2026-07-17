"""Tests for GET /devices -- create_app wired with a hand-written fake devices_client_factory, mirroring
test_trophy_routes.py's style.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from curator.psn.errors import PsnAuthError
from curator.psn.models import AccountDevice
from test_routes import EMAIL, SUB, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _seed_link


class FakeSocialClient:
    """Stands in for SocialClient: canned devices() result, or raises PsnAuthError when armed."""

    def __init__(self, *, raise_auth_error=False):
        self.raise_auth_error = raise_auth_error

    async def devices(self):
        if self.raise_auth_error:
            raise PsnAuthError("boom")
        return [
            AccountDevice(
                device_id="dev-1",
                device_type="PS5",
                device_name="My PS5",
                activation_type="PRIMARY",
                activation_date="2026-01-01T00:00:00Z",
                deactivation_date=None,
            )
        ]


class FakeSocialClientFactory:
    """Records every ``sub`` requested; raises ``RuntimeError`` for any ``sub`` not explicitly linked."""

    def __init__(self):
        self.linked: dict[str, FakeSocialClient] = {}
        self.calls: list[str] = []

    async def __call__(self, sub):
        self.calls.append(sub)
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(f"No PSN link for user {sub!r}; cannot fetch devices.")
        return client


def _build(devices_client_factory=None, repository=None):
    settings = _make_settings()
    repository = repository if repository is not None else FakeRepository()
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(sub=SUB, email=EMAIL))
    app = create_app(
        settings,
        repository=repository,
        token_validator=validator,
        devices_client_factory=devices_client_factory or FakeSocialClientFactory(),
    )
    return TestClient(app), app.state.devices_client_factory


def _build_linked(devices_client_factory=None):
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB, harvest_devices=True)
    return _build(devices_client_factory, repository=repository)


def test_get_devices_no_link_is_404():
    client, _ = _build()
    response = client.get("/devices", headers=_bearer("valid-token"))
    assert response.status_code == 404


def test_get_devices_harvest_devices_disabled_is_403():
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB, harvest_devices=False)
    client, _ = _build(repository=repository)

    response = client.get("/devices", headers=_bearer("valid-token"))
    assert response.status_code == 403


def test_get_devices_happy_path():
    factory = FakeSocialClientFactory()
    factory.linked[SUB] = FakeSocialClient()
    client, _ = _build_linked(factory)

    response = client.get("/devices", headers=_bearer("valid-token"))

    assert response.status_code == 200
    assert response.json() == {
        "devices": [
            {
                "device_id": "dev-1",
                "device_type": "PS5",
                "device_name": "My PS5",
                "activation_type": "PRIMARY",
                "activation_date": "2026-01-01T00:00:00Z",
                "deactivation_date": None,
            }
        ]
    }
    assert factory.calls == [SUB]


def test_get_devices_psn_auth_error_is_401():
    factory = FakeSocialClientFactory()
    factory.linked[SUB] = FakeSocialClient(raise_auth_error=True)
    client, _ = _build_linked(factory)

    response = client.get("/devices", headers=_bearer("valid-token"))
    assert response.status_code == 401
