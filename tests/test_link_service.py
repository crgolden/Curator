"""Tests for link_service: hand-written fake Repository + fake PSN agent, real TokenCrypto."""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from curator.link_service import (
    LINK_ERROR_AUTH_FAILED,
    LINK_ERROR_INVALID_NPSSO,
    LINK_ERROR_MISMATCH,
    LINK_ERROR_UNVERIFIED,
    LinkError,
    LinkResult,
    emails_match,
    link,
    normalize_email,
    unlink,
)
from curator.persistence.crypto import TokenCrypto
from curator.persistence.repository import LinkRecord
from curator.psn.errors import PsnAuthError
from curator.token_response import ACCESS_TOKEN_KEY, REFRESH_TOKEN_KEY
from test_values import (
    lowercase_token,
    new_account_id,
    new_blank,
    new_email_address,
    new_identity_sub,
    new_npsso,
    new_opaque_token,
    new_utc_instant,
)


class FakeRepository:
    """Stands in for Repository: in-memory dict of sub -> LinkRecord, with call recording."""

    def __init__(self) -> None:
        self.links: dict[str, LinkRecord] = {}
        self.upsert_link_calls: list[tuple] = []
        self.set_link_account_calls: list[tuple[str, str]] = []
        self.delete_calls: list[str] = []
        self.touch_verified_calls: list[str] = []

    async def get_link(self, sub):
        return self.links.get(sub)

    async def upsert_link(
        self, sub, token_response_enc, access_token_expires_at, refresh_token_expires_at, psn_account_id=None
    ):
        self.upsert_link_calls.append(
            (sub, token_response_enc, access_token_expires_at, refresh_token_expires_at, psn_account_id)
        )
        existing = self.links.get(sub)
        self.links[sub] = LinkRecord(
            psn_account_id=psn_account_id,
            token_response_enc=token_response_enc,
            access_token_expires_at=access_token_expires_at,
            refresh_token_expires_at=refresh_token_expires_at,
            linked_at=new_utc_instant(),
            updated_at=new_utc_instant(),
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
                last_verified_at=new_utc_instant(),
            )

    async def delete_link(self, sub):
        self.delete_calls.append(sub)
        self.links.pop(sub, None)


@dataclass(frozen=True)
class FakeAccount:
    account_id: str


class FakeAgent:
    """A fake PSN agent: whoami()/account_email_verified() return canned values; records the npsso used."""

    def __init__(self, sub, npsso=None, *, account=None, email_info=None, raise_on=None):
        self.sub = sub
        self.npsso = npsso
        self._account = account or FakeAccount(account_id=new_account_id())
        self._email_info = email_info
        self._raise_on = raise_on or ()

    async def whoami(self):
        if FakeAgent.whoami.__name__ in self._raise_on:
            raise PsnAuthError(lowercase_token())
        return self._account

    async def account_email_verified(self):
        if FakeAgent.account_email_verified.__name__ in self._raise_on:
            raise PsnAuthError(lowercase_token())
        return self._email_info


def _make_crypto() -> TokenCrypto:
    return TokenCrypto(TokenCrypto.generate_key())


async def _seed_link(repo: FakeRepository, crypto: TokenCrypto, sub: str) -> None:
    """Simulate a token store having already persisted tokens during whoami() (as DbTokenStore does)."""
    token_response = json.dumps({ACCESS_TOKEN_KEY: new_opaque_token(), REFRESH_TOKEN_KEY: new_opaque_token()})
    encrypted = crypto.encrypt(token_response.encode())
    await repo.upsert_link(
        sub,
        encrypted,
        new_utc_instant(),
        new_utc_instant(),
    )


def test_normalize_email_strips_and_lowercases():
    email = new_email_address()
    assert normalize_email(f"{new_blank()}{email.upper()}{new_blank()}") == email


def test_emails_match_requires_verified_and_equal():
    email = new_email_address()
    other_email = new_email_address()
    assert emails_match(email.upper(), email, True) is True
    assert emails_match(email, email, False) is False
    assert emails_match(email, other_email, True) is False


async def test_link_verified_match_links_and_sets_account_id():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = new_identity_sub()
    supplied_npsso = new_npsso()
    email = new_email_address()
    account_id = new_account_id()
    await _seed_link(repo, crypto, sub)

    async def agent_factory(sub_arg, npsso=None):
        assert sub_arg == sub
        assert npsso == supplied_npsso
        return FakeAgent(sub_arg, npsso, account=FakeAccount(account_id=account_id), email_info=(email, True))

    result = await link(
        sub,
        supplied_npsso,
        email.upper(),
        repository=repo,
        token_crypto=crypto,
        agent_factory=agent_factory,
    )

    assert isinstance(result, LinkResult)
    assert result.psn_account_id == account_id
    assert repo.set_link_account_calls == [(sub, account_id)]
    assert repo.delete_calls == []
    assert repo.links[sub].psn_account_id == account_id
    assert repo.touch_verified_calls == [sub]
    assert repo.links[sub].last_verified_at is not None


async def test_link_address_mismatch_clears_and_raises():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = new_identity_sub()
    psn_email = new_email_address()
    await _seed_link(repo, crypto, sub)

    async def agent_factory(sub_arg, npsso=None):
        return FakeAgent(sub_arg, npsso, email_info=(psn_email, True))

    with pytest.raises(LinkError) as exc_info:
        await link(
            sub,
            new_npsso(),
            new_email_address(),
            repository=repo,
            token_crypto=crypto,
            agent_factory=agent_factory,
        )

    assert exc_info.value.kind == LINK_ERROR_MISMATCH
    assert repo.delete_calls == [sub]
    assert repo.set_link_account_calls == []
    assert repo.touch_verified_calls == []
    assert sub not in repo.links


async def test_link_matching_but_unverified_clears_and_raises():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = new_identity_sub()
    email = new_email_address()
    await _seed_link(repo, crypto, sub)

    async def agent_factory(sub_arg, npsso=None):
        return FakeAgent(sub_arg, npsso, email_info=(email, False))

    with pytest.raises(LinkError) as exc_info:
        await link(
            sub,
            new_npsso(),
            email,
            repository=repo,
            token_crypto=crypto,
            agent_factory=agent_factory,
        )

    assert exc_info.value.kind == LINK_ERROR_UNVERIFIED
    assert repo.delete_calls == [sub]
    assert repo.set_link_account_calls == []
    assert repo.touch_verified_calls == []


async def test_link_none_email_clears_and_raises_unverified():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = new_identity_sub()
    await _seed_link(repo, crypto, sub)

    async def agent_factory(sub_arg, npsso=None):
        return FakeAgent(sub_arg, npsso, email_info=None)

    with pytest.raises(LinkError) as exc_info:
        await link(
            sub,
            new_npsso(),
            new_email_address(),
            repository=repo,
            token_crypto=crypto,
            agent_factory=agent_factory,
        )

    assert exc_info.value.kind == LINK_ERROR_UNVERIFIED
    assert repo.delete_calls == [sub]
    assert repo.set_link_account_calls == []
    assert repo.touch_verified_calls == []


async def test_link_psn_auth_error_clears_and_raises_auth_failed():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = new_identity_sub()
    await _seed_link(repo, crypto, sub)

    async def agent_factory(sub_arg, npsso=None):
        return FakeAgent(sub_arg, npsso, raise_on=(FakeAgent.whoami.__name__,))

    with pytest.raises(LinkError) as exc_info:
        await link(
            sub,
            new_npsso(),
            new_email_address(),
            repository=repo,
            token_crypto=crypto,
            agent_factory=agent_factory,
        )

    assert exc_info.value.kind == LINK_ERROR_AUTH_FAILED
    assert repo.delete_calls == [sub]
    assert repo.set_link_account_calls == []
    assert repo.touch_verified_calls == []


async def test_link_verified_match_with_no_refresh_token_still_links():
    """A normal exchange always requests access_type=offline and gets a refresh_token, but PSN could still
    theoretically omit one (rate limiting, an account-level restriction, ...). whoami()'s bootstrap persists
    that access-token-only session anyway (DbTokenStore.save() only requires access_token), so a verified email
    match must still complete the link -- it's usable until access_token_expires_at, after which
    reverify_link() will clear it and prompt for a fresh npsso.
    """
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = new_identity_sub()
    email = new_email_address()
    account_id = new_account_id()
    encrypted = crypto.encrypt(json.dumps({ACCESS_TOKEN_KEY: new_opaque_token()}).encode())
    await repo.upsert_link(
        sub,
        encrypted,
        new_utc_instant(),
        None,
    )

    async def agent_factory(sub_arg, npsso=None):
        return FakeAgent(sub_arg, npsso, account=FakeAccount(account_id=account_id), email_info=(email, True))

    result = await link(
        sub,
        new_npsso(),
        email,
        repository=repo,
        token_crypto=crypto,
        agent_factory=agent_factory,
    )

    assert isinstance(result, LinkResult)
    assert result.psn_account_id == account_id
    assert result.access_token_expires_at is not None
    assert result.refresh_token_expires_at is None
    assert repo.set_link_account_calls == [(sub, account_id)]
    assert repo.touch_verified_calls == [sub]
    assert repo.links[sub].psn_account_id == account_id


async def test_link_invalid_npsso_rejected_before_any_agent_call():
    repo = FakeRepository()
    crypto = _make_crypto()
    calls: list[str] = []

    async def agent_factory(sub_arg, npsso=None):
        calls.append(sub_arg)
        raise AssertionError("agent_factory must not be called for an invalid npsso")

    sub, malformed_npsso, email = new_identity_sub(), "{" + lowercase_token(), new_email_address()

    with pytest.raises(LinkError) as exc_info:
        await link(sub, malformed_npsso, email, repository=repo, token_crypto=crypto, agent_factory=agent_factory)

    assert exc_info.value.kind == LINK_ERROR_INVALID_NPSSO
    assert calls == []
    assert repo.set_link_account_calls == []
    assert repo.delete_calls == []


async def test_link_accepts_npsso_json_blob():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = new_identity_sub()
    email = new_email_address()
    account_id = new_account_id()
    npsso_blob = json.dumps({"npsso": new_npsso()})
    await _seed_link(repo, crypto, sub)

    async def agent_factory(sub_arg, npsso=None):
        assert npsso == npsso_blob
        return FakeAgent(sub_arg, npsso, account=FakeAccount(account_id=account_id), email_info=(email, True))

    result = await link(
        sub,
        npsso_blob,
        email,
        repository=repo,
        token_crypto=crypto,
        agent_factory=agent_factory,
    )
    assert result.psn_account_id == account_id


async def test_unlink_clears_the_link():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = new_identity_sub()
    await _seed_link(repo, crypto, sub)

    await unlink(sub, repository=repo, token_crypto=crypto)

    assert repo.delete_calls == [sub]
    assert sub not in repo.links
