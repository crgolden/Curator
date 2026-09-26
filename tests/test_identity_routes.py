"""Tests for GET /identity -- create_app wired with a hand-written fake identity_client_factory, mirroring
test_trophy_routes.py's style.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from audit_fakes import RecordingAuditRepository
from curator import identity_routes
from curator.app import create_app
from curator.audit.repository import ACTION_IDENTITY_FETCH, OUTCOME_COMPLETED, OUTCOME_FAILED
from curator.deps import HARVEST_IDENTITY, PREFERENCE_NOT_LINKED_DETAIL, preference_disabled_detail
from curator.identity_routes import IdentityResponse
from curator.persistence.crypto import TokenCrypto
from curator.psn.account_client import Account
from curator.psn.errors import PsnAuthError
from test_routes import FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _path, _seed_link
from test_values import (
    lowercase_token,
    new_account_id,
    new_email_address,
    new_identity_sub,
    new_online_id,
    new_opaque_token,
)

SUB = new_identity_sub()
EMAIL = new_email_address()
TOKEN = new_opaque_token()


class FakeAccountClient:
    """Stands in for AccountClient: canned whoami() result, or raises PsnAuthError when armed."""

    def __init__(self, *, raise_auth_error=False):
        self.raise_auth_error = raise_auth_error
        self.account = Account(
            account_id=new_account_id(), online_id=new_online_id(), region=lowercase_token().capitalize()
        )

    async def whoami(self):
        if self.raise_auth_error:
            raise PsnAuthError(new_opaque_token())
        return self.account


class FakeAccountClientFactory:
    """Records every ``sub`` requested; raises ``RuntimeError`` for any ``sub`` not explicitly linked."""

    def __init__(self):
        self.linked: dict[str, FakeAccountClient] = {}
        self.calls: list[str] = []

    async def __call__(self, sub):
        self.calls.append(sub)
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(sub)
        return client


def _build(identity_client_factory=None, repository=None):
    settings = _make_settings()
    repository = repository if repository is not None else FakeRepository()
    validator = FakeTokenValidator()
    validator.register(TOKEN, _claims(sub=SUB, email=EMAIL))
    app = create_app(
        settings,
        repository=repository,
        token_validator=validator,
        identity_client_factory=identity_client_factory or FakeAccountClientFactory(),
        audit_repository=RecordingAuditRepository(),
    )
    return TestClient(app), app.state.identity_client_factory


def _build_linked(identity_client_factory=None):
    repository = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    _seed_link(repository, crypto, SUB, harvest_identity=True)
    return _build(identity_client_factory, repository=repository)


def _get_identity(client):
    return client.get(_path(client, identity_routes.get_identity), headers=_bearer(TOKEN))


def test_get_identity_no_link_is_404():
    client, _ = _build()
    response = _get_identity(client)
    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL


def test_get_identity_harvest_identity_disabled_is_403():
    repository = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    _seed_link(repository, crypto, SUB, harvest_identity=False)
    client, _ = _build(repository=repository)

    response = _get_identity(client)
    assert response.status_code == 403
    assert response.json()["detail"] == preference_disabled_detail(HARVEST_IDENTITY)


def test_get_identity_happy_path():
    account_client = FakeAccountClient()
    factory = FakeAccountClientFactory()
    factory.linked[SUB] = account_client
    client, _ = _build_linked(factory)

    response = _get_identity(client)

    assert response.status_code == 200
    assert IdentityResponse.model_validate(response.json()) == IdentityResponse(
        account_id=account_client.account.account_id,
        online_id=account_client.account.online_id,
        region=account_client.account.region,
    )
    assert factory.calls == [SUB]
    assert client.app.state.audit_repository.outcomes == [(ACTION_IDENTITY_FETCH, OUTCOME_COMPLETED)]


def test_get_identity_psn_auth_error_is_401():
    factory = FakeAccountClientFactory()
    factory.linked[SUB] = FakeAccountClient(raise_auth_error=True)
    client, _ = _build_linked(factory)

    response = _get_identity(client)
    assert response.status_code == 401
    assert client.app.state.audit_repository.outcomes == [(ACTION_IDENTITY_FETCH, OUTCOME_FAILED)]


def test_get_identity_never_uses_the_token_when_the_history_row_cannot_be_written():
    factory = FakeAccountClientFactory()
    factory.linked[SUB] = FakeAccountClient()
    client, _ = _build_linked(factory)
    client.app.state.audit_repository.begin_error = RuntimeError(SUB)

    with pytest.raises(RuntimeError):
        _get_identity(client)

    assert factory.calls == []
