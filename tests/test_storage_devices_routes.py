"""Tests for POST/GET/PATCH/DELETE /storage-devices, attach/detach, and PUT/GET
/storage-devices/{device_id}/installs, using create_app() with a fake CollectionsRepository.
"""

from __future__ import annotations

from dataclasses import replace

from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from curator import storage_devices_routes
from curator.app import create_app
from curator.collections.repository import (
    STORAGE_KIND_M2,
    STORAGE_KIND_USB,
    StorageDevice,
    StorageDeviceKind,
    UserConsole,
)
from curator.persistence.crypto import TokenCrypto
from curator.psn.title_platform import PS5
from curator.storage_devices_routes import (
    INVALID_STORAGE_DEVICE_KIND_DETAIL,
    STORAGE_DEVICE_NOT_FOUND_DETAIL,
    StorageDeviceInstallRequest,
    StorageDeviceInstallResponse,
    StorageDeviceInstallsResponse,
    StorageDeviceRequest,
    StorageDeviceResponse,
    StorageDeviceUpdateRequest,
    unknown_console_detail,
)
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _path
from test_values import (
    lowercase_token,
    new_console_id,
    new_game_id,
    new_identity_sub,
    new_opaque_token,
    new_size_gb,
    new_storage_device_id,
)

SUB_A = new_identity_sub()
SUB_B = new_identity_sub()
TOKEN_A = new_opaque_token()

_DEVICES = TypeAdapter(list[StorageDeviceResponse])


class FakeCollectionsRepository:
    """Same ownership-tracked-separately shape as ``test_consoles_routes.FakeCollectionsRepository`` --
    ``StorageDevice`` does carry a real ``identity_sub`` field (unlike ``UserConsole``), but keeping the
    same pattern here avoids two different conventions across sibling test files."""

    def __init__(self, consoles=None, console_owners=None, devices=None):
        self._consoles: dict[str, UserConsole] = {c.console_id: c for c in (consoles or [])}
        self._console_owners: dict[str, str] = dict(console_owners or {})
        self._devices: dict[str, StorageDevice] = {d.device_id: d for d in (devices or [])}
        self._installs: dict[str, dict[str, bool]] = {}
        self.set_install_calls = []

    async def get_console(self, identity_sub, console_id):
        if self._console_owners.get(console_id) != identity_sub:
            return None
        return self._consoles.get(console_id)

    async def create_storage_device(self, identity_sub, *, name, kind, capacity_gb, buffer_gb=0.0, console_id=None):
        device = StorageDevice(
            device_id=new_storage_device_id(),
            identity_sub=identity_sub,
            console_id=console_id,
            name=name,
            kind=kind,
            capacity_gb=capacity_gb,
            buffer_gb=buffer_gb,
        )
        self._devices[device.device_id] = device
        return device

    async def list_storage_devices(self, identity_sub):
        return [d for d in self._devices.values() if d.identity_sub == identity_sub]

    async def get_storage_device(self, identity_sub, device_id):
        device = self._devices.get(device_id)
        if device is None or device.identity_sub != identity_sub:
            return None
        return device

    async def update_storage_device(self, identity_sub, device_id, *, name=None, capacity_gb=None, buffer_gb=None):
        existing = await self.get_storage_device(identity_sub, device_id)
        if existing is None:
            return None
        updated = StorageDevice(
            device_id=existing.device_id,
            identity_sub=existing.identity_sub,
            console_id=existing.console_id,
            name=existing.name if name is None else name,
            kind=existing.kind,
            capacity_gb=existing.capacity_gb if capacity_gb is None else capacity_gb,
            buffer_gb=existing.buffer_gb if buffer_gb is None else buffer_gb,
        )
        self._devices[device_id] = updated
        return updated

    async def delete_storage_device(self, identity_sub, device_id):
        existing = await self.get_storage_device(identity_sub, device_id)
        if existing is None:
            return False
        del self._devices[device_id]
        return True

    async def set_storage_device_attachment(self, identity_sub, device_id, console_id):
        existing = await self.get_storage_device(identity_sub, device_id)
        if existing is None:
            return None
        updated = StorageDevice(
            device_id=existing.device_id,
            identity_sub=existing.identity_sub,
            console_id=console_id,
            name=existing.name,
            kind=existing.kind,
            capacity_gb=existing.capacity_gb,
            buffer_gb=existing.buffer_gb,
        )
        self._devices[device_id] = updated
        return updated

    async def set_storage_device_install(self, device_id, game_id, installed):
        self.set_install_calls.append((device_id, game_id, installed))
        self._installs.setdefault(device_id, {})[game_id] = installed

    async def list_storage_device_installed_game_ids(self, device_id):
        return {game_id for game_id, installed in self._installs.get(device_id, {}).items() if installed}


def _console() -> UserConsole:
    return UserConsole(
        console_id=new_console_id(),
        name=lowercase_token(),
        platform=PS5,
        raw_capacity_gb=new_size_gb(),
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )


def _device(
    identity_sub: str, *, kind: StorageDeviceKind = STORAGE_KIND_USB, console_id: str | None = None
) -> StorageDevice:
    return StorageDevice(
        device_id=new_storage_device_id(),
        identity_sub=identity_sub,
        console_id=console_id,
        name=lowercase_token(),
        kind=kind,
        capacity_gb=new_size_gb(),
        buffer_gb=0.0,
    )


def _response(device: StorageDevice) -> StorageDeviceResponse:
    return StorageDeviceResponse(
        device_id=device.device_id,
        console_id=device.console_id,
        name=device.name,
        kind=device.kind,
        capacity_gb=device.capacity_gb,
        buffer_gb=device.buffer_gb,
        effective_capacity_gb=device.effective_capacity_gb,
    )


def _build(collections_repository=None):
    repository = FakeRepository()
    token_crypto = TokenCrypto(TokenCrypto.generate_key())
    validator = FakeTokenValidator()
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=validator,
        collections_repository=collections_repository or FakeCollectionsRepository(),
    )
    return TestClient(app), validator


def _device_path(client, handler, device: StorageDevice, **path_parameters) -> str:
    return _path(client, handler, device_id=device.device_id, **path_parameters)


def _install_body(installed: bool) -> dict[str, object]:
    return StorageDeviceInstallRequest(installed=installed).model_dump()


def test_requires_bearer_token():
    client, _validator = _build()
    body = StorageDeviceRequest(name=lowercase_token(), kind=STORAGE_KIND_USB, capacity_gb=new_size_gb()).model_dump()

    response = client.post(_path(client, storage_devices_routes.create_storage_device), json=body)

    assert response.status_code == 401


def test_creates_an_unattached_device():
    client, validator = _build()
    validator.register(TOKEN_A, _claims(sub=SUB_A))
    name = lowercase_token()
    capacity_gb = new_size_gb()

    response = client.post(
        _path(client, storage_devices_routes.create_storage_device),
        json=StorageDeviceRequest(name=name, kind=STORAGE_KIND_USB, capacity_gb=capacity_gb).model_dump(),
        headers=_bearer(TOKEN_A),
    )

    assert response.status_code == 201
    body = StorageDeviceResponse.model_validate(response.json())
    assert body == StorageDeviceResponse(
        device_id=body.device_id,
        console_id=None,
        name=name,
        kind=STORAGE_KIND_USB,
        capacity_gb=capacity_gb,
        buffer_gb=0.0,
        effective_capacity_gb=capacity_gb,
    )


def test_create_device_rejects_unknown_kind():
    client, validator = _build()
    validator.register(TOKEN_A, _claims(sub=SUB_A))
    unknown_kind = lowercase_token()
    body = StorageDeviceRequest(name=lowercase_token(), kind=unknown_kind, capacity_gb=new_size_gb()).model_dump()

    response = client.post(
        _path(client, storage_devices_routes.create_storage_device), json=body, headers=_bearer(TOKEN_A)
    )

    assert response.status_code == 400
    assert response.json()["detail"] == INVALID_STORAGE_DEVICE_KIND_DETAIL


def test_create_device_rejects_a_console_that_isnt_the_callers():
    console = _console()
    repo = FakeCollectionsRepository(consoles=[console], console_owners={console.console_id: SUB_B})
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.post(
        _path(client, storage_devices_routes.create_storage_device),
        json=StorageDeviceRequest(
            name=lowercase_token(), kind=STORAGE_KIND_USB, capacity_gb=new_size_gb(), console_id=console.console_id
        ).model_dump(),
        headers=_bearer(TOKEN_A),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == unknown_console_detail(console.console_id)


def test_lists_only_the_callers_own_devices():
    own_device = _device(SUB_A)
    repo = FakeCollectionsRepository(devices=[own_device, _device(SUB_B)])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.get(_path(client, storage_devices_routes.list_storage_devices), headers=_bearer(TOKEN_A))

    assert response.status_code == 200
    assert _DEVICES.validate_python(response.json()) == [_response(own_device)]


def test_get_device_404s_for_another_users_device():
    device = _device(SUB_B)
    repo = FakeCollectionsRepository(devices=[device])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.get(
        _device_path(client, storage_devices_routes.get_storage_device, device), headers=_bearer(TOKEN_A)
    )

    assert response.status_code == 404
    assert response.json()["detail"] == STORAGE_DEVICE_NOT_FOUND_DETAIL


def test_patches_a_device():
    device = _device(SUB_A)
    new_name = lowercase_token()
    repo = FakeCollectionsRepository(devices=[device])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.patch(
        _device_path(client, storage_devices_routes.update_storage_device, device),
        json=StorageDeviceUpdateRequest(name=new_name).model_dump(exclude_unset=True),
        headers=_bearer(TOKEN_A),
    )

    assert response.status_code == 200
    assert StorageDeviceResponse.model_validate(response.json()) == _response(replace(device, name=new_name))


def test_deletes_a_device():
    device = _device(SUB_A)
    repo = FakeCollectionsRepository(devices=[device])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.delete(
        _device_path(client, storage_devices_routes.delete_storage_device, device), headers=_bearer(TOKEN_A)
    )

    assert response.status_code == 204
    assert device.device_id not in repo._devices


def test_attaches_a_device_to_the_callers_own_console():
    console = _console()
    device = _device(SUB_A)
    repo = FakeCollectionsRepository(consoles=[console], console_owners={console.console_id: SUB_A}, devices=[device])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.put(
        _device_path(client, storage_devices_routes.attach_storage_device, device, console_id=console.console_id),
        headers=_bearer(TOKEN_A),
    )

    assert response.status_code == 200
    assert StorageDeviceResponse.model_validate(response.json()) == _response(
        replace(device, console_id=console.console_id)
    )


def test_attach_rejects_a_console_that_isnt_the_callers():
    console = _console()
    device = _device(SUB_A)
    repo = FakeCollectionsRepository(consoles=[console], console_owners={console.console_id: SUB_B}, devices=[device])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.put(
        _device_path(client, storage_devices_routes.attach_storage_device, device, console_id=console.console_id),
        headers=_bearer(TOKEN_A),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == unknown_console_detail(console.console_id)


def test_detaches_a_device():
    device = _device(SUB_A, console_id=new_console_id())
    repo = FakeCollectionsRepository(devices=[device])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.delete(
        _device_path(client, storage_devices_routes.detach_storage_device, device), headers=_bearer(TOKEN_A)
    )

    assert response.status_code == 200
    assert StorageDeviceResponse.model_validate(response.json()) == _response(replace(device, console_id=None))


def test_sets_install_state_on_an_m2_device_for_a_ps5_game():
    device = _device(SUB_A, kind=STORAGE_KIND_M2)
    game_id = new_game_id()
    repo = FakeCollectionsRepository(devices=[device])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.put(
        _device_path(client, storage_devices_routes.set_storage_device_install, device, game_id=game_id),
        json=_install_body(True),
        headers=_bearer(TOKEN_A),
    )

    assert response.status_code == 200
    assert StorageDeviceInstallResponse.model_validate(response.json()) == StorageDeviceInstallResponse(
        device_id=device.device_id, game_id=game_id, installed=True
    )
    assert repo.set_install_calls == [(device.device_id, game_id, True)]


def test_allows_installing_a_ps5_game_on_usb_storage():
    device = _device(SUB_A, kind=STORAGE_KIND_USB)
    game_id = new_game_id()
    repo = FakeCollectionsRepository(devices=[device])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.put(
        _device_path(client, storage_devices_routes.set_storage_device_install, device, game_id=game_id),
        json=_install_body(True),
        headers=_bearer(TOKEN_A),
    )

    assert response.status_code == 200
    assert repo.set_install_calls == [(device.device_id, game_id, True)]


def test_allows_installing_a_ps4_game_on_usb_storage():
    device = _device(SUB_A, kind=STORAGE_KIND_USB)
    game_id = new_game_id()
    repo = FakeCollectionsRepository(devices=[device])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.put(
        _device_path(client, storage_devices_routes.set_storage_device_install, device, game_id=game_id),
        json=_install_body(True),
        headers=_bearer(TOKEN_A),
    )

    assert response.status_code == 200
    assert repo.set_install_calls == [(device.device_id, game_id, True)]


def test_allows_uninstalling_a_game_from_usb_storage():
    device = _device(SUB_A, kind=STORAGE_KIND_USB)
    game_id = new_game_id()
    repo = FakeCollectionsRepository(devices=[device])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.put(
        _device_path(client, storage_devices_routes.set_storage_device_install, device, game_id=game_id),
        json=_install_body(False),
        headers=_bearer(TOKEN_A),
    )

    assert response.status_code == 200
    assert repo.set_install_calls == [(device.device_id, game_id, False)]


def test_install_device_404s_for_another_users_device():
    device = _device(SUB_B)
    repo = FakeCollectionsRepository(devices=[device])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.put(
        _device_path(client, storage_devices_routes.set_storage_device_install, device, game_id=new_game_id()),
        json=_install_body(True),
        headers=_bearer(TOKEN_A),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == STORAGE_DEVICE_NOT_FOUND_DETAIL


def test_gets_installed_game_ids_for_a_device():
    device = _device(SUB_A, kind=STORAGE_KIND_M2)
    installed_game_id = new_game_id()
    uninstalled_game_id = new_game_id()
    repo = FakeCollectionsRepository(devices=[device])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))
    client.put(
        _device_path(client, storage_devices_routes.set_storage_device_install, device, game_id=installed_game_id),
        json=_install_body(True),
        headers=_bearer(TOKEN_A),
    )
    client.put(
        _device_path(client, storage_devices_routes.set_storage_device_install, device, game_id=uninstalled_game_id),
        json=_install_body(False),
        headers=_bearer(TOKEN_A),
    )

    response = client.get(
        _device_path(client, storage_devices_routes.get_storage_device_installs, device), headers=_bearer(TOKEN_A)
    )

    assert response.status_code == 200
    assert StorageDeviceInstallsResponse.model_validate(response.json()) == StorageDeviceInstallsResponse(
        game_ids=[installed_game_id]
    )
