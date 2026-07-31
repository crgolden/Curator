"""Tests for POST/GET/PATCH/DELETE /storage-devices, attach/detach, and PUT/GET
/storage-devices/{device_id}/installs, using create_app() with fake CollectionsRepository/
LibraryRepository -- including the PS5-cannot-run-from-USB playability rejection.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.collections.repository import StorageDevice, UserConsole
from curator.persistence.crypto import TokenCrypto
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings


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
        self._next_id = 1

    async def get_console(self, identity_sub, console_id):
        if self._console_owners.get(console_id) != identity_sub:
            return None
        return self._consoles.get(console_id)

    async def create_storage_device(self, identity_sub, *, name, kind, capacity_gb, buffer_gb=0.0, console_id=None):
        device_id = f"d{self._next_id}"
        self._next_id += 1
        device = StorageDevice(
            device_id=device_id,
            identity_sub=identity_sub,
            console_id=console_id,
            name=name,
            kind=kind,
            capacity_gb=capacity_gb,
            buffer_gb=buffer_gb,
        )
        self._devices[device_id] = device
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


class FakeLibraryRepository:
    def __init__(self, native_ps5_by_game=None):
        self._native_ps5_by_game = dict(native_ps5_by_game or {})

    async def is_native_ps5(self, identity_sub, game_id):
        return self._native_ps5_by_game.get(game_id)


def _console(console_id="c1"):
    return UserConsole(
        console_id=console_id,
        name="My PS5",
        platform="PS5",
        raw_capacity_gb=825.0,
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )


def _device(device_id="d1", identity_sub="sub-a", kind="usb", console_id=None):
    return StorageDevice(
        device_id=device_id,
        identity_sub=identity_sub,
        console_id=console_id,
        name="My USB drive",
        kind=kind,
        capacity_gb=1000.0,
        buffer_gb=0.0,
    )


def _build(collections_repository=None, library_repository=None):
    repository = FakeRepository()
    token_crypto = TokenCrypto(Fernet.generate_key())
    validator = FakeTokenValidator()
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=validator,
        collections_repository=collections_repository or FakeCollectionsRepository(),
        library_repository=library_repository or FakeLibraryRepository(),
    )
    return TestClient(app), validator


def test_requires_bearer_token():
    client, _validator = _build()

    response = client.post("/storage-devices", json={"name": "d", "kind": "usb", "capacity_gb": 500.0})

    assert response.status_code == 401


def test_creates_an_unattached_device():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/storage-devices",
        json={"name": "Travel drive", "kind": "usb", "capacity_gb": 500.0},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["console_id"] is None
    assert body["effective_capacity_gb"] == 500.0


def test_create_device_rejects_unknown_kind():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/storage-devices",
        json={"name": "Odd drive", "kind": "sd-card", "capacity_gb": 64.0},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400


def test_create_device_rejects_a_console_that_isnt_the_callers():
    repo = FakeCollectionsRepository(consoles=[_console("c1")], console_owners={"c1": "sub-b"})
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/storage-devices",
        json={"name": "Drive", "kind": "usb", "capacity_gb": 500.0, "console_id": "c1"},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400


def test_lists_only_the_callers_own_devices():
    repo = FakeCollectionsRepository(devices=[_device("d1", "sub-a"), _device("d2", "sub-b")])
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/storage-devices", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert [d["device_id"] for d in response.json()] == ["d1"]


def test_get_device_404s_for_another_users_device():
    repo = FakeCollectionsRepository(devices=[_device("d1", "sub-b")])
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/storage-devices/d1", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_patches_a_device():
    repo = FakeCollectionsRepository(devices=[_device("d1", "sub-a")])
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.patch("/storage-devices/d1", json={"name": "Renamed"}, headers=_bearer("token-a"))

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["kind"] == "usb"  # untouched -- kind isn't a PATCH field


def test_deletes_a_device():
    repo = FakeCollectionsRepository(devices=[_device("d1", "sub-a")])
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete("/storage-devices/d1", headers=_bearer("token-a"))

    assert response.status_code == 204
    assert "d1" not in repo._devices


def test_attaches_a_device_to_the_callers_own_console():
    repo = FakeCollectionsRepository(
        consoles=[_console("c1")], console_owners={"c1": "sub-a"}, devices=[_device("d1", "sub-a")]
    )
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put("/storage-devices/d1/attach/c1", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json()["console_id"] == "c1"


def test_attach_rejects_a_console_that_isnt_the_callers():
    repo = FakeCollectionsRepository(
        consoles=[_console("c1")], console_owners={"c1": "sub-b"}, devices=[_device("d1", "sub-a")]
    )
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put("/storage-devices/d1/attach/c1", headers=_bearer("token-a"))

    assert response.status_code == 400


def test_detaches_a_device():
    repo = FakeCollectionsRepository(devices=[_device("d1", "sub-a", console_id="c1")])
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete("/storage-devices/d1/attach", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json()["console_id"] is None


def test_sets_install_state_on_an_m2_device_for_a_ps5_game():
    repo = FakeCollectionsRepository(devices=[_device("d1", "sub-a", kind="m2")])
    library = FakeLibraryRepository(native_ps5_by_game={"g1": True})
    client, validator = _build(repo, library)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put("/storage-devices/d1/installs/g1", json={"installed": True}, headers=_bearer("token-a"))

    assert response.status_code == 200
    assert repo.set_install_calls == [("d1", "g1", True)]


def test_rejects_installing_a_ps5_game_on_usb_storage():
    repo = FakeCollectionsRepository(devices=[_device("d1", "sub-a", kind="usb")])
    library = FakeLibraryRepository(native_ps5_by_game={"g1": True})
    client, validator = _build(repo, library)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put("/storage-devices/d1/installs/g1", json={"installed": True}, headers=_bearer("token-a"))

    assert response.status_code == 400
    assert "USB" in response.json()["detail"]
    assert repo.set_install_calls == []


def test_allows_installing_a_ps4_game_on_usb_storage():
    repo = FakeCollectionsRepository(devices=[_device("d1", "sub-a", kind="usb")])
    library = FakeLibraryRepository(native_ps5_by_game={"g1": False})
    client, validator = _build(repo, library)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put("/storage-devices/d1/installs/g1", json={"installed": True}, headers=_bearer("token-a"))

    assert response.status_code == 200
    assert repo.set_install_calls == [("d1", "g1", True)]


def test_allows_uninstalling_a_ps5_game_from_usb_storage_even_though_installing_would_be_rejected():
    # Clearing a stale/mismatched row must always be possible -- see the route's own docstring.
    repo = FakeCollectionsRepository(devices=[_device("d1", "sub-a", kind="usb")])
    library = FakeLibraryRepository(native_ps5_by_game={"g1": True})
    client, validator = _build(repo, library)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put("/storage-devices/d1/installs/g1", json={"installed": False}, headers=_bearer("token-a"))

    assert response.status_code == 200
    assert repo.set_install_calls == [("d1", "g1", False)]


def test_install_device_404s_for_another_users_device():
    repo = FakeCollectionsRepository(devices=[_device("d1", "sub-b")])
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put("/storage-devices/d1/installs/g1", json={"installed": True}, headers=_bearer("token-a"))

    assert response.status_code == 404


def test_gets_installed_game_ids_for_a_device():
    repo = FakeCollectionsRepository(devices=[_device("d1", "sub-a", kind="m2")])
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))
    client.put("/storage-devices/d1/installs/g1", json={"installed": True}, headers=_bearer("token-a"))
    client.put("/storage-devices/d1/installs/g2", json={"installed": False}, headers=_bearer("token-a"))

    response = client.get("/storage-devices/d1/installs", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json()["game_ids"] == ["g1"]
