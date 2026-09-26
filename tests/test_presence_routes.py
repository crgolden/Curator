"""Tests for GET /presence -- create_app wired with a hand-written fake presence_client_factory, mirroring
test_trophy_routes.py's style.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from audit_fakes import RecordingAuditRepository
from curator import presence_routes
from curator.app import create_app
from curator.audit.repository import ACTION_PRESENCE_FETCH, OUTCOME_COMPLETED, OUTCOME_FAILED
from curator.deps import HARVEST_PRESENCE, PREFERENCE_NOT_LINKED_DETAIL, preference_disabled_detail
from curator.persistence.crypto import TokenCrypto
from curator.presence_routes import PresenceResponse
from curator.psn.errors import PsnAuthError
from curator.psn.models import Presence
from curator.psn.title_platform import PS5
from test_routes import FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _path, _seed_link
from test_values import lowercase_token, new_email_address, new_game_title, new_identity_sub, new_opaque_token

SUB = new_identity_sub()
EMAIL = new_email_address()
TOKEN = new_opaque_token()


class FakePresenceClient:
    """Stands in for PresenceClient: canned presence() result, or raises PsnAuthError when armed."""

    def __init__(self, *, raise_auth_error=False):
        self.raise_auth_error = raise_auth_error
        self.current = Presence(
            online_status=lowercase_token(),
            platform=PS5,
            last_online_date=None,
            game_title=new_game_title(),
        )

    async def presence(self, online_id=None, account_id=None):
        if self.raise_auth_error:
            raise PsnAuthError(new_opaque_token())
        return self.current


class FakePresenceClientFactory:
    """Records every ``sub`` requested; raises ``RuntimeError`` for any ``sub`` not explicitly linked."""

    def __init__(self):
        self.linked: dict[str, FakePresenceClient] = {}
        self.calls: list[str] = []

    async def __call__(self, sub):
        self.calls.append(sub)
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(sub)
        return client


def _build(presence_client_factory=None, repository=None):
    settings = _make_settings()
    repository = repository if repository is not None else FakeRepository()
    validator = FakeTokenValidator()
    validator.register(TOKEN, _claims(sub=SUB, email=EMAIL))
    app = create_app(
        settings,
        repository=repository,
        token_validator=validator,
        presence_client_factory=presence_client_factory or FakePresenceClientFactory(),
        audit_repository=RecordingAuditRepository(),
    )
    return TestClient(app), app.state.presence_client_factory


def _build_linked(presence_client_factory=None):
    repository = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    _seed_link(repository, crypto, SUB, harvest_presence=True)
    return _build(presence_client_factory, repository=repository)


def _get_presence(client):
    return client.get(_path(client, presence_routes.get_presence), headers=_bearer(TOKEN))


def test_get_presence_no_link_is_404():
    client, _ = _build()
    response = _get_presence(client)
    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL


def test_get_presence_harvest_presence_disabled_is_403():
    repository = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    _seed_link(repository, crypto, SUB, harvest_presence=False)
    client, _ = _build(repository=repository)

    response = _get_presence(client)
    assert response.status_code == 403
    assert response.json()["detail"] == preference_disabled_detail(HARVEST_PRESENCE)


def test_get_presence_happy_path():
    presence_client = FakePresenceClient()
    factory = FakePresenceClientFactory()
    factory.linked[SUB] = presence_client
    client, _ = _build_linked(factory)

    response = _get_presence(client)

    assert response.status_code == 200
    assert PresenceResponse.model_validate(response.json()) == PresenceResponse(
        online_status=presence_client.current.online_status,
        platform=presence_client.current.platform,
        last_online_date=None,
        game_title=presence_client.current.game_title,
    )
    assert factory.calls == [SUB]
    assert client.app.state.audit_repository.outcomes == [(ACTION_PRESENCE_FETCH, OUTCOME_COMPLETED)]


def test_get_presence_psn_auth_error_is_401():
    factory = FakePresenceClientFactory()
    factory.linked[SUB] = FakePresenceClient(raise_auth_error=True)
    client, _ = _build_linked(factory)

    response = _get_presence(client)
    assert response.status_code == 401
    assert client.app.state.audit_repository.outcomes == [(ACTION_PRESENCE_FETCH, OUTCOME_FAILED)]


def test_get_presence_never_uses_the_token_when_the_history_row_cannot_be_written():
    factory = FakePresenceClientFactory()
    factory.linked[SUB] = FakePresenceClient()
    client, _ = _build_linked(factory)
    client.app.state.audit_repository.begin_error = RuntimeError(SUB)

    with pytest.raises(RuntimeError):
        _get_presence(client)

    assert factory.calls == []
