"""Tests for GET /devices -- create_app wired with a hand-written fake devices_client_factory, mirroring
test_trophy_routes.py's style.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from audit_fakes import RecordingAuditRepository
from curator import devices_routes
from curator.app import create_app
from curator.audit.repository import ACTION_DEVICES_FETCH, OUTCOME_COMPLETED, OUTCOME_FAILED
from curator.deps import HARVEST_DEVICES, PREFERENCE_NOT_LINKED_DETAIL, preference_disabled_detail
from curator.devices_routes import AccountDeviceResponse, DevicesResponse
from curator.persistence.crypto import TokenCrypto
from curator.psn.device_registrations import collapse_by_device_id as _collapse_by_device_id
from curator.psn.errors import PsnAuthError
from curator.psn.models import AccountDevice
from test_routes import FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _path, _seed_link
from test_values import (
    lowercase_token,
    new_console_id,
    new_device_id,
    new_email_address,
    new_identity_sub,
    new_opaque_token,
    new_positive_count,
    new_utc_instant,
)

SUB = new_identity_sub()
EMAIL = new_email_address()
TOKEN = new_opaque_token()


def _account_device() -> AccountDevice:
    return AccountDevice(
        device_id=new_device_id(),
        device_type=lowercase_token().upper(),
        device_name=lowercase_token(),
        activation_type=lowercase_token().upper(),
        activation_date=new_utc_instant().isoformat(),
        deactivation_date=None,
    )


def _expected_response(device: AccountDevice, linked_console_id: str | None) -> AccountDeviceResponse:
    return AccountDeviceResponse(
        device_id=device.device_id,
        device_type=device.device_type,
        device_name=device.device_name,
        activation_type=device.activation_type,
        activation_date=device.activation_date,
        deactivation_date=device.deactivation_date,
        linked_console_id=linked_console_id,
    )


class FakeSocialClient:
    """Stands in for SocialClient: canned devices() result, or raises PsnAuthError when armed."""

    def __init__(self, devices=None, *, raise_auth_error=False):
        self.device_list: list[AccountDevice] = list(devices) if devices is not None else [_account_device()]
        self.raise_auth_error = raise_auth_error

    async def devices(self):
        if self.raise_auth_error:
            raise PsnAuthError(new_opaque_token())
        return list(self.device_list)


class FakeSocialClientFactory:
    """Records every ``sub`` requested; raises ``RuntimeError`` for any ``sub`` not explicitly linked."""

    def __init__(self):
        self.linked: dict[str, FakeSocialClient] = {}
        self.calls: list[str] = []

    async def __call__(self, sub):
        self.calls.append(sub)
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(sub)
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
    validator.register(TOKEN, _claims(sub=SUB, email=EMAIL))
    app = create_app(
        settings,
        repository=repository,
        token_validator=validator,
        social_client_factory=social_client_factory or FakeSocialClientFactory(),
        collections_repository=collections_repository or FakeCollectionsRepository(),
        audit_repository=RecordingAuditRepository(),
    )
    return TestClient(app), app.state.social_client_factory


def _build_linked(social_client_factory=None, collections_repository=None):
    repository = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
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


def _get_devices(client):
    return client.get(_path(client, devices_routes.get_devices), headers=_bearer(TOKEN))


def test_get_devices_returns_one_row_per_device_after_collapsing():
    activation = new_utc_instant()
    first_registration = replace(_account_device(), activation_date=activation.isoformat())
    latest_registration = replace(
        first_registration,
        activation_date=(activation + timedelta(seconds=new_positive_count())).isoformat(),
    )
    factory = FakeSocialClientFactory()
    factory.linked[SUB] = FakeSocialClient([first_registration, latest_registration])
    client, _ = _build_linked(factory)

    response = _get_devices(client)

    assert DevicesResponse.model_validate(response.json()) == DevicesResponse(
        devices=[_expected_response(latest_registration, None)]
    ), "the response must never carry the same device_id twice"


def test_get_devices_annotates_a_linked_console_without_a_second_call():
    device = _account_device()
    console_id = new_console_id()
    factory = FakeSocialClientFactory()
    factory.linked[SUB] = FakeSocialClient([device])
    links = FakeCollectionsRepository({device.device_id: console_id})
    client, _ = _build_linked(factory, collections_repository=links)

    response = _get_devices(client)

    assert response.status_code == 200
    assert DevicesResponse.model_validate(response.json()).devices[0].linked_console_id == console_id


def test_get_devices_reports_no_link_as_null_rather_than_omitting_it():
    factory = FakeSocialClientFactory()
    factory.linked[SUB] = FakeSocialClient()
    client, _ = _build_linked(factory)

    response = _get_devices(client)

    assert response.status_code == 200
    device = DevicesResponse.model_validate(response.json()).devices[0]
    assert device.linked_console_id is None
    assert device.model_fields_set == set(AccountDeviceResponse.model_fields)


def test_get_devices_no_link_is_404():
    client, _ = _build()
    response = _get_devices(client)
    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL


def test_get_devices_harvest_devices_disabled_is_403():
    repository = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    _seed_link(repository, crypto, SUB, harvest_devices=False)
    client, _ = _build(repository=repository)

    response = _get_devices(client)
    assert response.status_code == 403
    assert response.json()["detail"] == preference_disabled_detail(HARVEST_DEVICES)


def test_get_devices_happy_path():
    device = _account_device()
    factory = FakeSocialClientFactory()
    factory.linked[SUB] = FakeSocialClient([device])
    client, _ = _build_linked(factory)

    response = _get_devices(client)

    assert response.status_code == 200
    body = DevicesResponse.model_validate(response.json())
    assert body == DevicesResponse(devices=[_expected_response(device, None)])
    assert body.devices[0].model_fields_set == set(AccountDeviceResponse.model_fields)
    assert factory.calls == [SUB]
    assert client.app.state.audit_repository.outcomes == [(ACTION_DEVICES_FETCH, OUTCOME_COMPLETED)]


def test_get_devices_psn_auth_error_is_401():
    factory = FakeSocialClientFactory()
    factory.linked[SUB] = FakeSocialClient(raise_auth_error=True)
    client, _ = _build_linked(factory)

    response = _get_devices(client)
    assert response.status_code == 401
    assert client.app.state.audit_repository.outcomes == [(ACTION_DEVICES_FETCH, OUTCOME_FAILED)]


def test_get_devices_never_uses_the_token_when_the_history_row_cannot_be_written():
    factory = FakeSocialClientFactory()
    factory.linked[SUB] = FakeSocialClient()
    client, _ = _build_linked(factory)
    client.app.state.audit_repository.begin_error = RuntimeError(SUB)

    with pytest.raises(RuntimeError):
        _get_devices(client)

    assert factory.calls == []
