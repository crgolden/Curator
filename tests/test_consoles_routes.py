"""Tests for PUT /consoles/{console_id}/installs/{game_id}, using create_app() with a fake
CollectionsRepository -- including the ownership check that keeps one user from setting install state on
another user's console.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.collections.repository import UserConsole
from curator.persistence.crypto import TokenCrypto
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings


class FakeCollectionsRepository:
    def __init__(self, consoles=None):
        self._consoles = consoles or []
        self.set_install_calls = []

    async def list_user_consoles(self, identity_sub):
        return self._consoles

    async def set_console_install(self, console_id, game_id, installed):
        self.set_install_calls.append((console_id, game_id, installed))


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


def test_sets_install_state_for_owned_console():
    repo = FakeCollectionsRepository(consoles=[_console("c1")])
    client, validator = _build(repo)
    validator.register("token-a", _claims())

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
    # The console belongs to sub-b's library; sub-a authenticates and tries to write to it anyway.
    repo = FakeCollectionsRepository(consoles=[])  # empty: list_user_consoles is called with sub-a, not sub-b
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put(
        "/consoles/other-users-console/installs/g1", json={"installed": True}, headers=_bearer("token-a")
    )

    assert response.status_code == 404
    assert repo.set_install_calls == []
