"""Tests for GET/PUT/DELETE /me/enrichment-keys -- create_app wired with a hand-written
FakeEnrichmentKeysRepository, same DI-seam style as test_preferences_routes.py.
"""

from __future__ import annotations

import logging
from datetime import datetime

import httpx
from fastapi.testclient import TestClient

from curator import enrichment_keys_routes
from curator.app import create_app
from curator.audit.repository import ACTION_ENRICHMENT_KEY_ADDED, ACTION_ENRICHMENT_KEY_REMOVED
from curator.enrichment.rawg_client import REDACTED_PLACEHOLDER
from curator.enrichment_keys_routes import (
    OPENCRITIC_PROVIDER,
    RAWG_PROVIDER,
    EnrichmentKeyStatusResponse,
    SetEnrichmentKeyRequest,
)
from curator.persistence.crypto import TokenCrypto
from curator.persistence.enrichment_keys_repository import EnrichmentKeyStatus
from test_routes import (
    FakeAuditRepository,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _path,
)
from test_values import lowercase_token, new_email_address, new_identity_sub, new_opaque_token, new_utc_instant

SUB = new_identity_sub()
EMAIL = new_email_address()
TOKEN = new_opaque_token()

_FIXED_REJECTED_AT = new_utc_instant()


class FakeEnrichmentKeysRepository:
    """Stands in for EnrichmentKeysRepository: in-memory dict of sub -> (rawg_enc, oc_enc), with call
    recording."""

    def __init__(self) -> None:
        self.rawg: dict[str, bytes] = {}
        self.opencritic: dict[str, bytes] = {}
        self.rawg_rejected_at: dict[str, datetime] = {}
        self.opencritic_rejected_at: dict[str, datetime] = {}
        self.upsert_rawg_calls: list[tuple[str, bytes]] = []
        self.upsert_opencritic_calls: list[tuple[str, bytes]] = []
        self.delete_rawg_calls: list[str] = []
        self.delete_opencritic_calls: list[str] = []

    async def get_status(self, sub: str) -> EnrichmentKeyStatus:
        return EnrichmentKeyStatus(
            rawg_configured=sub in self.rawg,
            opencritic_configured=sub in self.opencritic,
            rawg_added_at=None,
            opencritic_added_at=None,
            rawg_key_rejected_at=self.rawg_rejected_at.get(sub),
            opencritic_key_rejected_at=self.opencritic_rejected_at.get(sub),
        )

    async def mark_rawg_key_rejected(self, sub: str) -> None:
        self.rawg_rejected_at[sub] = _FIXED_REJECTED_AT

    async def mark_opencritic_key_rejected(self, sub: str) -> None:
        self.opencritic_rejected_at[sub] = _FIXED_REJECTED_AT

    async def get_decrypted_key_material(self, sub: str):
        return self.rawg.get(sub), self.opencritic.get(sub)

    async def upsert_rawg_key(self, sub: str, key_enc: bytes) -> None:
        self.rawg[sub] = key_enc
        self.rawg_rejected_at.pop(sub, None)
        self.upsert_rawg_calls.append((sub, key_enc))

    async def upsert_opencritic_key(self, sub: str, key_enc: bytes) -> None:
        self.opencritic[sub] = key_enc
        self.opencritic_rejected_at.pop(sub, None)
        self.upsert_opencritic_calls.append((sub, key_enc))

    async def delete_rawg_key(self, sub: str) -> None:
        self.rawg.pop(sub, None)
        self.delete_rawg_calls.append(sub)

    async def delete_opencritic_key(self, sub: str) -> None:
        self.opencritic.pop(sub, None)
        self.delete_opencritic_calls.append(sub)


def _mock_http_client(status_code: int = 200) -> httpx.AsyncClient:
    """A client that answers every request with ``status_code`` -- used to stand in for RAWG/OpenCritic's
    add-time key-validation call without touching the network. Validation reads only the status."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code)

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


def _build(enrichment_keys_repository=None, audit_repository=None, http_client=None):
    settings = _make_settings()
    repository = FakeRepository()
    token_crypto = TokenCrypto(TokenCrypto.generate_key())
    enrichment_keys_repository = enrichment_keys_repository or FakeEnrichmentKeysRepository()
    audit_repository = audit_repository if audit_repository is not None else FakeAuditRepository()
    validator = FakeTokenValidator()
    validator.register(TOKEN, _claims(sub=SUB, email=EMAIL))
    app = create_app(
        settings,
        repository=repository,
        token_crypto=token_crypto,
        token_validator=validator,
        enrichment_keys_repository=enrichment_keys_repository,
        audit_repository=audit_repository,
        http_client=http_client or _mock_http_client(),
    )
    return TestClient(app), enrichment_keys_repository, audit_repository


def _status_path(client) -> str:
    return _path(client, enrichment_keys_routes.get_enrichment_key_status)


def _key_path(client, provider: str) -> str:
    return _path(client, enrichment_keys_routes.set_enrichment_key, provider=provider)


def _key_body(api_key: str) -> dict[str, object]:
    return SetEnrichmentKeyRequest(api_key=api_key).model_dump()


def _status(client) -> EnrichmentKeyStatusResponse:
    return EnrichmentKeyStatusResponse.model_validate(client.get(_status_path(client), headers=_bearer(TOKEN)).json())


def test_get_status_never_404s_with_no_keys():
    client, _, _ = _build()
    response = client.get(_status_path(client), headers=_bearer(TOKEN))

    assert response.status_code == 200
    status = EnrichmentKeyStatusResponse.model_validate(response.json())
    assert status == EnrichmentKeyStatusResponse(
        rawg_configured=False,
        opencritic_configured=False,
        rawg_added_at=None,
        opencritic_added_at=None,
        rawg_key_rejected_at=None,
        opencritic_key_rejected_at=None,
    )
    assert status.model_fields_set == set(EnrichmentKeyStatusResponse.model_fields)


def test_get_status_surfaces_a_rejected_rawg_key_even_though_it_is_still_configured():
    client, repo, _ = _build()
    client.put(_key_path(client, RAWG_PROVIDER), json=_key_body(new_opaque_token()), headers=_bearer(TOKEN))
    repo.rawg_rejected_at[SUB] = _FIXED_REJECTED_AT

    status = _status(client)

    assert status.rawg_configured is True
    assert status.rawg_key_rejected_at == _FIXED_REJECTED_AT.isoformat()
    assert status.opencritic_key_rejected_at is None


def test_re_saving_a_rawg_key_clears_a_prior_rejection():
    client, repo, _ = _build()
    client.put(_key_path(client, RAWG_PROVIDER), json=_key_body(new_opaque_token()), headers=_bearer(TOKEN))
    repo.rawg_rejected_at[SUB] = _FIXED_REJECTED_AT

    client.put(_key_path(client, RAWG_PROVIDER), json=_key_body(new_opaque_token()), headers=_bearer(TOKEN))

    assert _status(client).rawg_key_rejected_at is None


def test_put_rawg_key_encrypts_and_stores_then_status_reflects_it():
    api_key = new_opaque_token()
    client, repo, audit = _build()
    response = client.put(_key_path(client, RAWG_PROVIDER), json=_key_body(api_key), headers=_bearer(TOKEN))

    assert response.status_code == 204
    sub, key_enc = repo.upsert_rawg_calls[0]
    assert sub == SUB
    assert key_enc != api_key.encode()
    assert audit.entries == [(SUB, ACTION_ENRICHMENT_KEY_ADDED, RAWG_PROVIDER)]

    status = _status(client)
    assert status.rawg_configured is True
    assert status.opencritic_configured is False


def test_put_opencritic_key_is_independent_of_rawg():
    client, _repo, _ = _build()
    client.put(_key_path(client, OPENCRITIC_PROVIDER), json=_key_body(new_opaque_token()), headers=_bearer(TOKEN))

    status = _status(client)
    assert status.rawg_configured is False
    assert status.opencritic_configured is True


def test_put_empty_key_is_rejected():
    client, repo, _ = _build()
    response = client.put(_key_path(client, RAWG_PROVIDER), json=_key_body("   "), headers=_bearer(TOKEN))

    assert response.status_code == 400
    assert repo.upsert_rawg_calls == []


def test_put_unknown_provider_is_422():
    client, _, _ = _build()
    response = client.put(
        _key_path(client, lowercase_token()), json=_key_body(new_opaque_token()), headers=_bearer(TOKEN)
    )
    assert response.status_code == 422


def test_response_never_echoes_the_key_value():
    api_key = new_opaque_token()
    client, _, _ = _build()
    response = client.put(_key_path(client, RAWG_PROVIDER), json=_key_body(api_key), headers=_bearer(TOKEN))

    assert api_key not in response.text
    status_response = client.get(_status_path(client), headers=_bearer(TOKEN))
    assert api_key not in status_response.text


def test_delete_rawg_key_leaves_opencritic_intact():
    client, repo, audit = _build()
    client.put(_key_path(client, RAWG_PROVIDER), json=_key_body(new_opaque_token()), headers=_bearer(TOKEN))
    client.put(_key_path(client, OPENCRITIC_PROVIDER), json=_key_body(new_opaque_token()), headers=_bearer(TOKEN))

    response = client.delete(
        _path(client, enrichment_keys_routes.delete_enrichment_key, provider=RAWG_PROVIDER), headers=_bearer(TOKEN)
    )

    assert response.status_code == 204
    assert repo.delete_rawg_calls == [SUB]
    assert audit.entries[-1] == (SUB, ACTION_ENRICHMENT_KEY_REMOVED, RAWG_PROVIDER)

    status = _status(client)
    assert status.rawg_configured is False
    assert status.opencritic_configured is True


def test_put_rawg_key_rejected_by_provider_is_400_and_not_persisted():
    client, repo, _ = _build(http_client=_mock_http_client(401))
    response = client.put(_key_path(client, RAWG_PROVIDER), json=_key_body(new_opaque_token()), headers=_bearer(TOKEN))

    assert response.status_code == 400
    assert repo.upsert_rawg_calls == []
    assert _status(client).rawg_configured is False


def test_provider_rejection_logs_the_providers_own_explanation_never_the_key(caplog):
    api_key = new_opaque_token()
    provider_explanation = lowercase_token()

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"{provider_explanation} {api_key}")

    client, _, _ = _build(http_client=httpx.AsyncClient(transport=httpx.MockTransport(_handler)))

    with caplog.at_level(logging.WARNING, logger=enrichment_keys_routes.logger.name):
        response = client.put(_key_path(client, RAWG_PROVIDER), json=_key_body(api_key), headers=_bearer(TOKEN))

    assert response.status_code == 400
    assert f"{provider_explanation} {REDACTED_PLACEHOLDER}" in caplog.text
    assert api_key not in caplog.text


def test_put_opencritic_key_rejected_by_provider_is_400_and_not_persisted():
    client, repo, _ = _build(http_client=_mock_http_client(403))
    response = client.put(
        _key_path(client, OPENCRITIC_PROVIDER), json=_key_body(new_opaque_token()), headers=_bearer(TOKEN)
    )

    assert response.status_code == 400
    assert repo.upsert_opencritic_calls == []


def test_put_rawg_key_provider_unreachable_is_503_and_not_persisted():
    client, repo, _ = _build(http_client=_mock_http_client(500))
    response = client.put(_key_path(client, RAWG_PROVIDER), json=_key_body(new_opaque_token()), headers=_bearer(TOKEN))

    assert response.status_code == 503
    assert repo.upsert_rawg_calls == []


def test_requires_bearer_token():
    client, _, _ = _build()
    delete_path = _path(client, enrichment_keys_routes.delete_enrichment_key, provider=RAWG_PROVIDER)
    assert client.get(_status_path(client)).status_code == 401
    assert client.put(_key_path(client, RAWG_PROVIDER), json=_key_body(new_opaque_token())).status_code == 401
    assert client.delete(delete_path).status_code == 401
