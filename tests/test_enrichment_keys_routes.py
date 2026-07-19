"""Tests for GET/PUT/DELETE /me/enrichment-keys -- create_app wired with a hand-written
FakeEnrichmentKeysRepository, same DI-seam style as test_preferences_routes.py.
"""

from __future__ import annotations

import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from curator.persistence.enrichment_keys_repository import EnrichmentKeyStatus
from test_routes import (
    EMAIL,
    SUB,
    FakeAuditRepository,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
)


class FakeEnrichmentKeysRepository:
    """Stands in for EnrichmentKeysRepository: in-memory dict of sub -> (rawg_enc, oc_enc), with call
    recording."""

    def __init__(self) -> None:
        self.rawg: dict[str, bytes] = {}
        self.opencritic: dict[str, bytes] = {}
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
        )

    async def get_decrypted_key_material(self, sub: str):
        return self.rawg.get(sub), self.opencritic.get(sub)

    async def upsert_rawg_key(self, sub: str, key_enc: bytes) -> None:
        self.rawg[sub] = key_enc
        self.upsert_rawg_calls.append((sub, key_enc))

    async def upsert_opencritic_key(self, sub: str, key_enc: bytes) -> None:
        self.opencritic[sub] = key_enc
        self.upsert_opencritic_calls.append((sub, key_enc))

    async def delete_rawg_key(self, sub: str) -> None:
        self.rawg.pop(sub, None)
        self.delete_rawg_calls.append(sub)

    async def delete_opencritic_key(self, sub: str) -> None:
        self.opencritic.pop(sub, None)
        self.delete_opencritic_calls.append(sub)


def _mock_http_client(status_code: int = 200) -> httpx.AsyncClient:
    """A client that answers every request with ``status_code`` -- used to stand in for RAWG/OpenCritic's
    add-time key-validation call without touching the network."""

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=[] if "opencritic" in request.url.host else {"results": []})

    return httpx.AsyncClient(transport=httpx.MockTransport(_handler))


def _build(enrichment_keys_repository=None, audit_repository=None, http_client=None):
    settings = _make_settings()
    repository = FakeRepository()
    token_crypto = TokenCrypto(Fernet.generate_key())
    enrichment_keys_repository = enrichment_keys_repository or FakeEnrichmentKeysRepository()
    audit_repository = audit_repository if audit_repository is not None else FakeAuditRepository()
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(sub=SUB, email=EMAIL))
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


def test_get_status_never_404s_with_no_keys():
    client, _, _ = _build()
    response = client.get("/me/enrichment-keys", headers=_bearer("valid-token"))

    assert response.status_code == 200
    assert response.json() == {
        "rawg_configured": False,
        "opencritic_configured": False,
        "rawg_added_at": None,
        "opencritic_added_at": None,
    }


def test_put_rawg_key_encrypts_and_stores_then_status_reflects_it():
    client, repo, audit = _build()
    response = client.put("/me/enrichment-keys/rawg", json={"api_key": "my-rawg-key"}, headers=_bearer("valid-token"))

    assert response.status_code == 204
    sub, key_enc = repo.upsert_rawg_calls[0]
    assert sub == SUB
    assert key_enc != b"my-rawg-key"  # encrypted, not stored raw
    assert audit.entries == [(SUB, "enrichment_key_added", "rawg")]

    status = client.get("/me/enrichment-keys", headers=_bearer("valid-token")).json()
    assert status["rawg_configured"] is True
    assert status["opencritic_configured"] is False


def test_put_opencritic_key_is_independent_of_rawg():
    client, _repo, _ = _build()
    client.put("/me/enrichment-keys/opencritic", json={"api_key": "my-oc-key"}, headers=_bearer("valid-token"))

    status = client.get("/me/enrichment-keys", headers=_bearer("valid-token")).json()
    assert status["rawg_configured"] is False
    assert status["opencritic_configured"] is True


def test_put_empty_key_is_rejected():
    client, repo, _ = _build()
    response = client.put("/me/enrichment-keys/rawg", json={"api_key": "   "}, headers=_bearer("valid-token"))

    assert response.status_code == 400
    assert repo.upsert_rawg_calls == []


def test_put_unknown_provider_is_422():
    client, _, _ = _build()
    response = client.put("/me/enrichment-keys/steam", json={"api_key": "x"}, headers=_bearer("valid-token"))
    assert response.status_code == 422


def test_response_never_echoes_the_key_value():
    client, _, _ = _build()
    response = client.put(
        "/me/enrichment-keys/rawg", json={"api_key": "super-secret-key"}, headers=_bearer("valid-token")
    )

    assert "super-secret-key" not in response.text
    status_response = client.get("/me/enrichment-keys", headers=_bearer("valid-token"))
    assert "super-secret-key" not in status_response.text


def test_delete_rawg_key_leaves_opencritic_intact():
    client, repo, audit = _build()
    client.put("/me/enrichment-keys/rawg", json={"api_key": "rawg-key"}, headers=_bearer("valid-token"))
    client.put("/me/enrichment-keys/opencritic", json={"api_key": "oc-key"}, headers=_bearer("valid-token"))

    response = client.delete("/me/enrichment-keys/rawg", headers=_bearer("valid-token"))

    assert response.status_code == 204
    assert repo.delete_rawg_calls == [SUB]
    assert audit.entries[-1] == (SUB, "enrichment_key_removed", "rawg")

    status = client.get("/me/enrichment-keys", headers=_bearer("valid-token")).json()
    assert status["rawg_configured"] is False
    assert status["opencritic_configured"] is True


def test_put_rawg_key_rejected_by_provider_is_400_and_not_persisted():
    client, repo, _ = _build(http_client=_mock_http_client(401))
    response = client.put("/me/enrichment-keys/rawg", json={"api_key": "bad-key"}, headers=_bearer("valid-token"))

    assert response.status_code == 400
    assert repo.upsert_rawg_calls == []
    status = client.get("/me/enrichment-keys", headers=_bearer("valid-token")).json()
    assert status["rawg_configured"] is False


def test_put_opencritic_key_rejected_by_provider_is_400_and_not_persisted():
    client, repo, _ = _build(http_client=_mock_http_client(403))
    response = client.put("/me/enrichment-keys/opencritic", json={"api_key": "bad-key"}, headers=_bearer("valid-token"))

    assert response.status_code == 400
    assert repo.upsert_opencritic_calls == []


def test_put_rawg_key_provider_unreachable_is_503_and_not_persisted():
    client, repo, _ = _build(http_client=_mock_http_client(500))
    response = client.put("/me/enrichment-keys/rawg", json={"api_key": "some-key"}, headers=_bearer("valid-token"))

    assert response.status_code == 503
    assert repo.upsert_rawg_calls == []


def test_requires_bearer_token():
    client, _, _ = _build()
    assert client.get("/me/enrichment-keys").status_code == 401
    assert client.put("/me/enrichment-keys/rawg", json={"api_key": "x"}).status_code == 401
    assert client.delete("/me/enrichment-keys/rawg").status_code == 401
