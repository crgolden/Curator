"""Tests for POST/GET/PATCH/DELETE /consoles and PUT/GET /consoles/{console_id}/installs, using
create_app() with a fake CollectionsRepository -- including the ownership checks that keep one user from
reading/writing another user's console.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.collections.repository import UserConsole
from curator.persistence.crypto import TokenCrypto
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings


class FakeCollectionsRepository:
    """Tracks console ownership separately from ``UserConsole`` itself -- the real dataclass has no
    ``identity_sub`` field (the real query already scopes by it in SQL), so this fake keeps
    ``console_id -> identity_sub`` alongside ``console_id -> UserConsole`` rather than smuggling an extra
    attribute onto a frozen, slotted dataclass."""

    def __init__(self, consoles=None, owners=None):
        self._consoles: dict[str, UserConsole] = {c.console_id: c for c in (consoles or [])}
        self._owners: dict[str, str] = dict(owners or {c.console_id: "sub-a" for c in (consoles or [])})
        self._installs: dict[str, dict[str, bool]] = {}
        self.set_install_calls = []
        self._next_id = 1

    async def get_console(self, identity_sub, console_id):
        if self._owners.get(console_id) != identity_sub:
            return None
        return self._consoles.get(console_id)

    async def create_console(
        self,
        identity_sub,
        *,
        name,
        platform,
        raw_capacity_gb,
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
        model=None,
    ):
        console_id = f"c{self._next_id}"
        self._next_id += 1
        console = UserConsole(
            console_id=console_id,
            name=name,
            platform=platform,
            raw_capacity_gb=raw_capacity_gb,
            update_buffer_gb=update_buffer_gb,
            routing_genres=routing_genres,
            fill_order=fill_order,
            model=model,
        )
        self._consoles[console_id] = console
        self._owners[console_id] = identity_sub
        return console

    async def update_console(
        self,
        identity_sub,
        console_id,
        *,
        name=None,
        raw_capacity_gb=None,
        update_buffer_gb=None,
        routing_genres=None,
        fill_order=None,
    ):
        existing = await self.get_console(identity_sub, console_id)
        if existing is None:
            return None
        updated = UserConsole(
            console_id=existing.console_id,
            name=existing.name if name is None else name,
            platform=existing.platform,
            raw_capacity_gb=existing.raw_capacity_gb if raw_capacity_gb is None else raw_capacity_gb,
            update_buffer_gb=existing.update_buffer_gb if update_buffer_gb is None else update_buffer_gb,
            routing_genres=existing.routing_genres if routing_genres is None else routing_genres,
            fill_order=existing.fill_order if fill_order is None else fill_order,
        )
        self._consoles[console_id] = updated
        return updated

    async def delete_console(self, identity_sub, console_id):
        existing = await self.get_console(identity_sub, console_id)
        if existing is None:
            return False
        del self._consoles[console_id]
        del self._owners[console_id]
        return True

    async def set_console_install(self, console_id, game_id, installed):
        self.set_install_calls.append((console_id, game_id, installed))
        self._installs.setdefault(console_id, {})[game_id] = installed

    async def list_installed_game_ids(self, console_id):
        return {game_id for game_id, installed in self._installs.get(console_id, {}).items() if installed}


def _console(console_id="c1"):
    return UserConsole(
        console_id=console_id,
        name="My PS5",
        platform="PS5",
        raw_capacity_gb=100.0,
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )


def _build(collections_repository=None):
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
    )
    return TestClient(app), validator


def test_requires_bearer_token():
    client, _validator = _build()

    response = client.put("/consoles/c1/installs/g1", json={"installed": True})

    assert response.status_code == 401


def test_creates_a_console():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/consoles",
        json={"name": "Living room PS5", "platform": "PS5", "raw_capacity_gb": 825.0},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Living room PS5"
    assert body["platform"] == "PS5"
    assert body["effective_capacity_gb"] == 825.0


def test_creates_a_console_with_no_capacity_using_a_known_model_default():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/consoles",
        json={"name": "Living room PS5", "platform": "PS5", "model": "PS5 Digital Edition"},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["raw_capacity_gb"] == 667.0
    assert body["capacity_is_default"] is True


def test_creates_a_console_with_no_capacity_and_no_model_using_the_platform_fallback():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/consoles", json={"name": "Mystery PS5", "platform": "PS5"}, headers=_bearer("token-a"))

    assert response.status_code == 201
    body = response.json()
    assert body["raw_capacity_gb"] == 667.0
    assert body["capacity_is_default"] is True
    assert body["model"] is None


def test_creates_a_console_with_explicit_capacity_is_never_flagged_as_default():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/consoles",
        json={"name": "Measured PS5", "platform": "PS5", "raw_capacity_gb": 700.0},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["raw_capacity_gb"] == 700.0
    assert body["capacity_is_default"] is False


def test_get_console_never_reports_capacity_as_default_even_if_it_was_originally():
    repo = FakeCollectionsRepository(consoles=[_console("c1")], owners={"c1": "sub-a"})
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/consoles/c1", headers=_bearer("token-a"))

    assert response.json()["capacity_is_default"] is False


def test_create_console_rejects_unknown_platform():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/consoles",
        json={"name": "Odd console", "platform": "Switch", "raw_capacity_gb": 32.0},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400


def test_gets_one_owned_console():
    repo = FakeCollectionsRepository(consoles=[_console("c1")], owners={"c1": "sub-a"})
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/consoles/c1", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json()["console_id"] == "c1"


def test_get_console_404s_for_another_users_console():
    repo = FakeCollectionsRepository(consoles=[_console("c1")], owners={"c1": "sub-b"})
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/consoles/c1", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_patches_a_console():
    repo = FakeCollectionsRepository(consoles=[_console("c1")], owners={"c1": "sub-a"})
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.patch("/consoles/c1", json={"name": "Renamed"}, headers=_bearer("token-a"))

    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Renamed"
    assert body["platform"] == "PS5"  # untouched -- platform isn't a PATCH field at all


def test_patch_console_404s_for_another_users_console():
    repo = FakeCollectionsRepository(consoles=[_console("c1")], owners={"c1": "sub-b"})
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.patch("/consoles/c1", json={"name": "Renamed"}, headers=_bearer("token-a"))

    assert response.status_code == 404


def test_deletes_a_console():
    repo = FakeCollectionsRepository(consoles=[_console("c1")], owners={"c1": "sub-a"})
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete("/consoles/c1", headers=_bearer("token-a"))

    assert response.status_code == 204
    assert "c1" not in repo._consoles


def test_delete_console_404s_for_another_users_console():
    repo = FakeCollectionsRepository(consoles=[_console("c1")], owners={"c1": "sub-b"})
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete("/consoles/c1", headers=_bearer("token-a"))

    assert response.status_code == 404
    assert "c1" in repo._consoles


def test_sets_install_state_for_owned_console():
    repo = FakeCollectionsRepository(consoles=[_console("c1")], owners={"c1": "sub-a"})
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put("/consoles/c1/installs/g1", json={"installed": True}, headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == {"console_id": "c1", "game_id": "g1", "installed": True}
    assert repo.set_install_calls == [("c1", "g1", True)]


def test_unknown_console_is_404():
    repo = FakeCollectionsRepository(consoles=[])
    client, validator = _build(repo)
    validator.register("token-a", _claims())

    response = client.put("/consoles/c1/installs/g1", json={"installed": True}, headers=_bearer("token-a"))

    assert response.status_code == 404
    assert repo.set_install_calls == []


def test_cannot_set_install_state_on_another_users_console():
    repo = FakeCollectionsRepository(
        consoles=[_console("other-users-console")], owners={"other-users-console": "sub-b"}
    )
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put(
        "/consoles/other-users-console/installs/g1", json={"installed": True}, headers=_bearer("token-a")
    )

    assert response.status_code == 404
    assert repo.set_install_calls == []


def test_gets_installed_game_ids_hydrating_from_the_server():
    repo = FakeCollectionsRepository(consoles=[_console("c1")], owners={"c1": "sub-a"})
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))
    client.put("/consoles/c1/installs/g1", json={"installed": True}, headers=_bearer("token-a"))
    client.put("/consoles/c1/installs/g2", json={"installed": True}, headers=_bearer("token-a"))
    client.put("/consoles/c1/installs/g3", json={"installed": False}, headers=_bearer("token-a"))

    response = client.get("/consoles/c1/installs", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert sorted(response.json()["game_ids"]) == ["g1", "g2"]


def test_get_installs_404s_for_another_users_console():
    repo = FakeCollectionsRepository(consoles=[_console("c1")], owners={"c1": "sub-b"})
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/consoles/c1/installs", headers=_bearer("token-a"))

    assert response.status_code == 404
