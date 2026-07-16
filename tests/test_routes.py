"""Tests for the FastAPI app: create_app wiring, bearer-token auth, ``/me`` (incl. re-verify), and psn
link/unlink routes. Every collaborator (repository, crypto, agent_factory, token_validator) is a
hand-written fake -- no ``unittest.mock``, matching the persistence-layer test style.

Curator is a pure JWT Bearer resource server: there is no session, no cookie, no login/callback route.
Every request presents an ``Authorization: Bearer <token>`` header; ``FakeTokenValidator`` maps known
token strings to canned :class:`~curator.token_validation.TokenClaims`, and raises
:class:`~curator.token_validation.TokenError` for anything else -- standing in for
:class:`~curator.token_validation.JwtValidator` the same way the persistence-layer fakes stand in for
psycopg.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from curator.persistence.repository import LinkRecord
from curator.psn.errors import PsnAuthError
from curator.settings import Settings
from curator.token_validation import TokenClaims, TokenError

SUB = "sub-1"
EMAIL = "user@example.com"

# A fixed "now" `touch_link_verified` stamps on to a link -- deliberately far from any `iat` used below so
# "does this token's iat come before/after the link's last_verified_at" comparisons are unambiguous.
TOUCHED_AT = datetime(2027, 1, 1, tzinfo=timezone.utc)

OLD_IAT = datetime(2026, 1, 1, tzinfo=timezone.utc)
NEW_IAT = datetime(2026, 6, 1, tzinfo=timezone.utc)


class FakeRepository:
    """Stands in for Repository: in-memory dict of sub -> LinkRecord, with call recording."""

    def __init__(self) -> None:
        self.links: dict[str, LinkRecord] = {}
        self.users: set[str] = set()
        self.login_touches: list[str] = []
        self.delete_calls: list[str] = []
        self.set_link_account_calls: list[tuple[str, str]] = []
        self.touch_verified_calls: list[str] = []

    async def upsert_user(self, sub):
        self.users.add(sub)

    async def touch_login(self, sub):
        self.login_touches.append(sub)

    async def get_link(self, sub):
        return self.links.get(sub)

    async def upsert_link(
        self, sub, token_response_enc, access_token_expires_at, refresh_token_expires_at, psn_account_id=None
    ):
        existing = self.links.get(sub)
        self.links[sub] = LinkRecord(
            psn_account_id=psn_account_id
            if psn_account_id is not None
            else (existing.psn_account_id if existing else None),
            token_response_enc=token_response_enc,
            access_token_expires_at=access_token_expires_at,
            refresh_token_expires_at=refresh_token_expires_at,
            linked_at=existing.linked_at if existing else datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            last_verified_at=existing.last_verified_at if existing else None,
        )

    async def set_link_account(self, sub, psn_account_id):
        self.set_link_account_calls.append((sub, psn_account_id))
        existing = self.links.get(sub)
        if existing is not None:
            self.links[sub] = LinkRecord(
                psn_account_id=psn_account_id,
                token_response_enc=existing.token_response_enc,
                access_token_expires_at=existing.access_token_expires_at,
                refresh_token_expires_at=existing.refresh_token_expires_at,
                linked_at=existing.linked_at,
                updated_at=existing.updated_at,
                last_verified_at=existing.last_verified_at,
            )

    async def touch_link_verified(self, sub):
        self.touch_verified_calls.append(sub)
        existing = self.links.get(sub)
        if existing is not None:
            self.links[sub] = LinkRecord(
                psn_account_id=existing.psn_account_id,
                token_response_enc=existing.token_response_enc,
                access_token_expires_at=existing.access_token_expires_at,
                refresh_token_expires_at=existing.refresh_token_expires_at,
                linked_at=existing.linked_at,
                updated_at=existing.updated_at,
                last_verified_at=TOUCHED_AT,
            )

    async def delete_link(self, sub):
        self.delete_calls.append(sub)
        self.links.pop(sub, None)


def _seed_link(
    repo: FakeRepository, crypto: TokenCrypto, sub: str, account_id: str = "psn-account-1", last_verified_at=None
) -> None:
    """Seed a pre-existing PSN link, as if a previous /psn/link call (or DbTokenStore.save) had run."""
    encrypted = crypto.encrypt(b'{"access_token": "AT", "refresh_token": "RT"}')
    repo.links[sub] = LinkRecord(
        psn_account_id=account_id,
        token_response_enc=encrypted,
        access_token_expires_at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        refresh_token_expires_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        linked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_verified_at=last_verified_at,
    )


class FakeAgent:
    """A fake PSN agent. ``whoami()`` mimics production's side effect of persisting tokens through the
    injected token store (here: writing straight into the fake repository) so route tests exercise the
    same "expirations become visible after linking" behavior a real DbTokenStore-backed agent would."""

    def __init__(self, sub, repository, token_crypto, *, email_info, account_id, raise_kind):
        self._sub = sub
        self._repository = repository
        self._token_crypto = token_crypto
        self._email_info = email_info
        self._account_id = account_id
        self._raise_kind = raise_kind

    async def whoami(self):
        if self._raise_kind == "whoami":
            raise PsnAuthError("boom")
        encrypted = self._token_crypto.encrypt(b'{"access_token": "AT", "refresh_token": "RT"}')
        await self._repository.upsert_link(
            self._sub,
            encrypted,
            datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
            datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        return SimpleNamespace(account_id=self._account_id)

    async def account_email_verified(self):
        if self._raise_kind == "email":
            raise PsnAuthError("boom")
        return self._email_info


class FakeAgentFactory:
    """Records every call and hands out configurable FakeAgents."""

    def __init__(self, repository, token_crypto):
        self.repository = repository
        self.token_crypto = token_crypto
        self.email_info = (EMAIL, True)
        self.account_id = "psn-account-1"
        self.raise_kind = None
        self.calls: list[tuple[str, str | None]] = []

    async def __call__(self, sub, npsso=None):
        self.calls.append((sub, npsso))
        return FakeAgent(
            sub,
            self.repository,
            self.token_crypto,
            email_info=self.email_info,
            account_id=self.account_id,
            raise_kind=self.raise_kind,
        )


class FakeTokenValidator:
    """Stands in for JwtValidator: maps known token strings to canned TokenClaims; anything else raises
    TokenError, exactly like a real signature/issuer/expiry failure would."""

    def __init__(self):
        self.tokens: dict[str, TokenClaims] = {}

    def register(self, token: str, claims: TokenClaims) -> None:
        self.tokens[token] = claims

    def validate(self, token: str) -> TokenClaims:
        claims = self.tokens.get(token)
        if claims is None:
            raise TokenError("Unknown or invalid token.")
        return claims


def _claims(sub=SUB, email=EMAIL, iat=NEW_IAT, scopes=("curator",), is_admin=False) -> TokenClaims:
    return TokenClaims(sub=sub, email=email, iat=iat, scopes=scopes, is_admin=is_admin)


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _make_settings() -> Settings:
    return Settings(
        oidc_authority="https://identity.example.test",
        token_key=Fernet.generate_key().decode(),
        database_url="postgresql://unused",
    )


def _build(repository=None, token_crypto=None, agent_factory=None, token_validator=None):
    settings = _make_settings()
    repository = repository if repository is not None else FakeRepository()
    token_crypto = token_crypto if token_crypto is not None else TokenCrypto(Fernet.generate_key())
    agent_factory = agent_factory if agent_factory is not None else FakeAgentFactory(repository, token_crypto)
    token_validator = token_validator if token_validator is not None else FakeTokenValidator()
    app = create_app(
        settings,
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=agent_factory,
        token_validator=token_validator,
    )
    client = TestClient(app)
    return client, repository, token_crypto, agent_factory, token_validator


def _build_with_valid_token(token="valid-token", **claims_kwargs):
    client, repo, crypto, agent_factory, validator = _build()
    validator.register(token, _claims(**claims_kwargs))
    return client, repo, crypto, agent_factory, validator


def test_create_app_returns_a_fastapi_instance():
    client, *_ = _build()
    from fastapi import FastAPI

    assert isinstance(client.app, FastAPI)


def test_health_returns_plain_text_healthy():
    client, *_ = _build()
    response = client.get("/health")
    assert response.status_code == 200
    assert response.text == "Healthy"


def test_create_app_with_no_redis_settings_disables_caching_and_rate_limiting():
    """Redis unset (the current default in every environment) must not prevent the app from starting --
    matching the "optional leg" philosophy every other collaborator (RAWG, OpenCritic, Service Bus,
    telemetry) already follows."""
    client, *_ = _build()

    assert client.app.state.redis_client is None
    assert client.app.state.trophy_client_factory is not None


async def test_trophy_client_factory_raises_for_unlinked_user():
    client, *_ = _build()

    with pytest.raises(RuntimeError, match="No PSN link"):
        await client.app.state.trophy_client_factory("no-such-sub")


async def test_create_app_wires_injected_redis_client_into_rate_limiter_and_trophy_cache():
    """A caller-supplied ``redis_client`` (the DI seam every other collaborator gets) must flow into both
    the rate limiter used by every PSN session and the trophy-client factory's caching wrapper."""
    from curator.psn.trophy_cache import CachedTrophyClient
    from test_redis_client import FakeRawRedis

    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB)
    settings = _make_settings()
    fake_redis = FakeRawRedis()

    app = create_app(
        settings,
        repository=repository,
        token_crypto=crypto,
        agent_factory=FakeAgentFactory(repository, crypto),
        token_validator=FakeTokenValidator(),
        redis_client=fake_redis,
    )

    assert app.state.redis_client is fake_redis
    trophy_client = await app.state.trophy_client_factory(SUB)
    assert isinstance(trophy_client, CachedTrophyClient)


def test_me_without_bearer_token_is_401():
    client, *_ = _build()
    response = client.get("/me")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_me_with_malformed_authorization_header_is_401():
    client, *_ = _build()
    response = client.get("/me", headers={"Authorization": "Basic dXNlcjpwYXNz"})
    assert response.status_code == 401


def test_me_with_invalid_token_is_401():
    client, *_ = _build()
    response = client.get("/me", headers=_bearer("garbage-not-a-real-token"))
    assert response.status_code == 401


def test_me_without_curator_scope_is_403():
    client, *_ = _build_with_valid_token(scopes=("openid",))
    response = client.get("/me", headers=_bearer("valid-token"))
    assert response.status_code == 403


def test_authenticated_request_upserts_caller_and_touches_login():
    """require_bearer must upsert the caller's app_users row on every authenticated request -- psn_links
    (and every other account-scoped table) has a REFERENCES app_users(identity_sub) foreign key, so any
    downstream write for a sub that was never upserted would fail at the database.
    """
    client, repo, *_ = _build_with_valid_token()
    response = client.get("/me", headers=_bearer("valid-token"))
    assert response.status_code == 200
    assert SUB in repo.users
    assert repo.login_touches == [SUB]


def test_request_without_curator_scope_never_upserts_caller():
    client, repo, *_ = _build_with_valid_token(scopes=("openid",))
    response = client.get("/me", headers=_bearer("valid-token"))
    assert response.status_code == 403
    assert repo.users == set()
    assert repo.login_touches == []


def test_me_without_email_claim_is_403():
    client, *_ = _build_with_valid_token(email=None)
    response = client.get("/me", headers=_bearer("valid-token"))
    assert response.status_code == 403
    assert response.json()["detail"] == "email claim required"


def test_me_reports_unlinked():
    client, *_ = _build_with_valid_token()
    response = client.get("/me", headers=_bearer("valid-token"))
    assert response.status_code == 200
    body = response.json()
    assert body["sub"] == SUB
    assert body["email"] == EMAIL
    assert body["linked"] is False
    assert body["psn"] is None


def test_me_with_matching_verified_link_keeps_it_and_touches_verified():
    repo = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    agent_factory = FakeAgentFactory(repo, crypto)
    agent_factory.email_info = (EMAIL, True)
    _seed_link(repo, crypto, SUB, last_verified_at=OLD_IAT)
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(iat=NEW_IAT))
    client, *_ = _build(repository=repo, token_crypto=crypto, agent_factory=agent_factory, token_validator=validator)

    response = client.get("/me", headers=_bearer("valid-token"))

    assert response.status_code == 200
    assert repo.delete_calls == []
    assert repo.touch_verified_calls == [SUB]
    assert response.json()["linked"] is True


def test_me_with_mismatched_email_auto_unlinks():
    repo = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    agent_factory = FakeAgentFactory(repo, crypto)
    agent_factory.email_info = ("someone-else@example.com", True)
    _seed_link(repo, crypto, SUB, last_verified_at=OLD_IAT)
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(iat=NEW_IAT))
    client, *_ = _build(repository=repo, token_crypto=crypto, agent_factory=agent_factory, token_validator=validator)

    response = client.get("/me", headers=_bearer("valid-token"))

    assert repo.delete_calls == [SUB]
    assert response.json()["linked"] is False


def test_me_with_unverified_email_auto_unlinks():
    repo = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    agent_factory = FakeAgentFactory(repo, crypto)
    agent_factory.email_info = (EMAIL, False)
    _seed_link(repo, crypto, SUB, last_verified_at=OLD_IAT)
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(iat=NEW_IAT))
    client, *_ = _build(repository=repo, token_crypto=crypto, agent_factory=agent_factory, token_validator=validator)

    response = client.get("/me", headers=_bearer("valid-token"))

    assert repo.delete_calls == [SUB]
    assert response.json()["linked"] is False


def test_me_reverify_network_blip_leaves_link_intact():
    repo = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())

    class FlakyAgent(FakeAgent):
        async def account_email_verified(self):
            raise RuntimeError("transient network blip")

    calls: list[tuple[str, str | None]] = []

    def factory(sub, npsso=None):
        calls.append((sub, npsso))
        return FlakyAgent(sub, repo, crypto, email_info=None, account_id="x", raise_kind=None)

    _seed_link(repo, crypto, SUB, last_verified_at=OLD_IAT)
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(iat=NEW_IAT))
    client, *_ = _build(repository=repo, token_crypto=crypto, agent_factory=factory, token_validator=validator)

    response = client.get("/me", headers=_bearer("valid-token"))

    assert repo.delete_calls == []
    assert repo.touch_verified_calls == []
    assert response.json()["linked"] is True


def test_me_reverify_skips_psn_check_when_token_iat_not_newer_than_last_verified():
    repo = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    agent_factory = FakeAgentFactory(repo, crypto)
    _seed_link(repo, crypto, SUB, last_verified_at=NEW_IAT)
    validator = FakeTokenValidator()
    # OLD_IAT is not newer than the link's last_verified_at (NEW_IAT) -- must not re-trigger a PSN check.
    validator.register("valid-token", _claims(iat=OLD_IAT))
    client, *_ = _build(repository=repo, token_crypto=crypto, agent_factory=agent_factory, token_validator=validator)

    response = client.get("/me", headers=_bearer("valid-token"))

    assert response.status_code == 200
    assert agent_factory.calls == []
    assert repo.delete_calls == []
    assert repo.touch_verified_calls == []
    assert response.json()["linked"] is True


def test_psn_link_without_bearer_token_is_401():
    client, *_ = _build()
    response = client.post("/psn/link", json={"npsso": "some-token"})
    assert response.status_code == 401


def test_psn_link_without_curator_scope_is_403():
    client, *_ = _build_with_valid_token(scopes=())
    response = client.post("/psn/link", json={"npsso": "x"}, headers=_bearer("valid-token"))
    assert response.status_code == 403


def test_psn_link_without_email_claim_is_403():
    client, *_ = _build_with_valid_token(email=None)
    response = client.post("/psn/link", json={"npsso": "x"}, headers=_bearer("valid-token"))
    assert response.status_code == 403
    assert response.json()["detail"] == "email claim required"


def test_psn_link_happy_path_then_me_shows_linked_with_expirations():
    client, repo, _crypto, agent_factory, _validator = _build_with_valid_token()
    agent_factory.email_info = (EMAIL, True)

    response = client.post("/psn/link", json={"npsso": "the-npsso"}, headers=_bearer("valid-token"))

    assert response.status_code == 200
    body = response.json()
    assert body["linked"] is True
    assert body["psn"]["access_token_expires_at"] is not None
    assert body["psn"]["refresh_token_expires_at"] is not None
    assert agent_factory.calls[-1] == (SUB, "the-npsso")
    assert repo.touch_verified_calls == [SUB]

    me_response = client.get("/me", headers=_bearer("valid-token"))
    me_body = me_response.json()
    assert me_body["linked"] is True
    assert me_body["psn"]["access_token_expires_at"] is not None


def test_psn_link_mismatch_returns_409():
    client, _repo, _crypto, agent_factory, _validator = _build_with_valid_token()
    agent_factory.email_info = ("someone-else@example.com", True)

    response = client.post("/psn/link", json={"npsso": "the-npsso"}, headers=_bearer("valid-token"))

    assert response.status_code == 409
    assert response.json()["detail"] == {"error": "mismatch", "message": "emails do not match"}


def test_psn_link_unverified_returns_409():
    client, _repo, _crypto, agent_factory, _validator = _build_with_valid_token()
    agent_factory.email_info = (EMAIL, False)

    response = client.post("/psn/link", json={"npsso": "the-npsso"}, headers=_bearer("valid-token"))

    assert response.status_code == 409
    assert response.json()["detail"] == {"error": "unverified", "message": "PSN email is not verified"}


def test_psn_link_invalid_npsso_returns_400():
    client, _repo, _crypto, agent_factory, _validator = _build_with_valid_token()

    response = client.post("/psn/link", json={"npsso": "{not valid json"}, headers=_bearer("valid-token"))

    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_npsso"
    assert agent_factory.calls == []


def test_psn_link_auth_failure_returns_401():
    client, _repo, _crypto, agent_factory, _validator = _build_with_valid_token()
    agent_factory.raise_kind = "whoami"

    response = client.post("/psn/link", json={"npsso": "the-npsso"}, headers=_bearer("valid-token"))

    assert response.status_code == 401
    assert response.json()["detail"] == {"error": "auth_failed", "message": "PSN authentication failed"}


def test_psn_unlink_then_me_shows_unlinked():
    repo = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    agent_factory = FakeAgentFactory(repo, crypto)
    agent_factory.email_info = (EMAIL, True)
    _seed_link(repo, crypto, SUB)
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims())
    client, *_ = _build(repository=repo, token_crypto=crypto, agent_factory=agent_factory, token_validator=validator)

    response = client.delete("/psn/link", headers=_bearer("valid-token"))
    assert response.status_code == 204

    me_response = client.get("/me", headers=_bearer("valid-token"))
    assert me_response.json()["linked"] is False


def test_psn_unlink_without_bearer_token_is_401():
    client, *_ = _build()
    response = client.delete("/psn/link")
    assert response.status_code == 401


def test_psn_unlink_without_email_claim_is_403():
    client, *_ = _build_with_valid_token(email=None)
    response = client.delete("/psn/link", headers=_bearer("valid-token"))
    assert response.status_code == 403
