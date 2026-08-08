"""Tests for GET /devices -- create_app wired with a hand-written fake devices_client_factory, mirroring
test_trophy_routes.py's style.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.devices_routes import _collapse_by_device_id
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


class FakeCollectionsRepository:
    def __init__(self, console_id_by_device=None):
        self.console_id_by_device = dict(console_id_by_device or {})

    async def list_console_device_links(self, identity_sub):
        return dict(self.console_id_by_device)


def _build(social_client_factory=None, repository=None, collections_repository=None):
    settings = _make_settings()
    repository = repository if repository is not None else FakeRepository()
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(sub=SUB, email=EMAIL))
    app = create_app(
        settings,
        repository=repository,
        token_validator=validator,
        social_client_factory=social_client_factory or FakeSocialClientFactory(),
        collections_repository=collections_repository or FakeCollectionsRepository(),
    )
    return TestClient(app), app.state.social_client_factory


def _build_linked(social_client_factory=None, collections_repository=None):
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB, harvest_devices=True)
    return _build(social_client_factory, repository=repository, collections_repository=collections_repository)


def test_repeated_registrations_of_one_device_collapse_to_a_single_entry():
    """PSN returns one row per activation, so a re-registered device repeats under one device_id.

    Observed live: two PS3 rows 267ms apart plus a third months later, all sharing one device_id.
    """
    devices = [
        AccountDevice(
            device_id="dev-ps3",
            device_type="PS3",
            device_name="PlayStation 3",
            activation_type="PRIMARY",
            activation_date="2026-03-09T14:12:31.064Z",
            deactivation_date=None,
        ),
        AccountDevice(
            device_id="dev-ps3",
            device_type="PS3",
            device_name=None,
            activation_type="PRIMARY",
            activation_date="2026-05-22T13:47:16.339Z",
            deactivation_date=None,
        ),
        AccountDevice(
            device_id="dev-ps5",
            device_type="PS5",
            device_name="PlayStation 5",
            activation_type="PRIMARY",
            activation_date="2026-03-09T14:46:16.408Z",
            deactivation_date=None,
        ),
    ]

    collapsed = _collapse_by_device_id(devices)

    assert [d.device_id for d in collapsed] == ["dev-ps3", "dev-ps5"]
    assert collapsed[0].activation_date == "2026-05-22T13:47:16.339Z", "the most recent activation wins"
    assert collapsed[0].device_name == "PlayStation 3", "a name from an older row beats the newer row's null"


def test_collapsing_preserves_first_seen_order():
    devices = [
        AccountDevice("b", "PS5", "B", "PRIMARY", "2026-01-01T00:00:00Z", None),
        AccountDevice("a", "PS4", "A", "PRIMARY", "2026-01-02T00:00:00Z", None),
        AccountDevice("b", "PS5", "B", "PRIMARY", "2026-02-01T00:00:00Z", None),
    ]

    assert [d.device_id for d in _collapse_by_device_id(devices)] == ["b", "a"]


def test_devices_without_an_id_are_kept_rather_than_merged_together():
    devices = [
        AccountDevice(None, "PS3", "One", "PRIMARY", "2026-01-01T00:00:00Z", None),
        AccountDevice(None, "PS3", "Two", "PRIMARY", "2026-01-02T00:00:00Z", None),
    ]

    collapsed = _collapse_by_device_id(devices)

    assert len(collapsed) == 2, "nothing identifies these well enough to treat them as the same device"


def test_get_devices_returns_one_row_per_device_after_collapsing():
    factory = FakeSocialClientFactory()
    factory.linked[SUB] = FakeSocialClient()
    client, _ = _build_linked(factory)

    response = client.get("/devices", headers=_bearer("valid-token"))

    ids = [d["device_id"] for d in response.json()["devices"]]
    assert len(ids) == len(set(ids)), "the response must never carry the same device_id twice"


def test_get_devices_annotates_a_linked_console_without_a_second_call():
    factory = FakeSocialClientFactory()
    factory.linked[SUB] = FakeSocialClient()
    links = FakeCollectionsRepository({"dev-1": "console-a"})
    client, _ = _build_linked(factory, collections_repository=links)

    response = client.get("/devices", headers=_bearer("valid-token"))

    assert response.status_code == 200
    devices = response.json()["devices"]
    assert devices[0]["linked_console_id"] == "console-a"


def test_get_devices_reports_no_link_as_null_rather_than_omitting_it():
    factory = FakeSocialClientFactory()
    factory.linked[SUB] = FakeSocialClient()
    client, _ = _build_linked(factory)

    response = client.get("/devices", headers=_bearer("valid-token"))

    assert response.status_code == 200
    assert response.json()["devices"][0]["linked_console_id"] is None


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
                "linked_console_id": None,
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
