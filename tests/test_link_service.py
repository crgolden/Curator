"""Tests for link_service: hand-written fake Repository + fake PSN agent, real TokenCrypto."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet

from curator.link_service import LinkError, LinkResult, emails_match, link, normalize_email, unlink
from curator.persistence.crypto import TokenCrypto
from curator.persistence.repository import LinkRecord
from curator.psn.errors import PsnAuthError


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
            linked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
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
                last_verified_at=datetime(2026, 1, 1, 12, tzinfo=timezone.utc),
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
        self._account = account or FakeAccount(account_id="psn-account-1")
        self._email_info = email_info
        self._raise_on = raise_on or ()

    async def whoami(self):
        if "whoami" in self._raise_on:
            raise PsnAuthError("boom")
        return self._account

    async def account_email_verified(self):
        if "account_email_verified" in self._raise_on:
            raise PsnAuthError("boom")
        return self._email_info


def _make_crypto() -> TokenCrypto:
    return TokenCrypto(Fernet.generate_key())


async def _seed_link(repo: FakeRepository, crypto: TokenCrypto, sub: str) -> None:
    """Simulate a token store having already persisted tokens during whoami() (as DbTokenStore does)."""
    encrypted = crypto.encrypt(b'{"access_token": "AT", "refresh_token": "RT"}')
    await repo.upsert_link(
        sub,
        encrypted,
        datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        datetime(2026, 2, 1, tzinfo=timezone.utc),
    )


def test_normalize_email_strips_and_lowercases():
    assert normalize_email("  Foo@Example.COM  ") == "foo@example.com"


def test_emails_match_requires_verified_and_equal():
    assert emails_match("Foo@Example.com", "foo@example.com", True) is True
    assert emails_match("foo@example.com", "foo@example.com", False) is False
    assert emails_match("foo@example.com", "bar@example.com", True) is False


async def test_link_verified_match_links_and_sets_account_id():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = "sub-1"
    await _seed_link(repo, crypto, sub)

    async def agent_factory(sub_arg, npsso=None):
        assert sub_arg == sub
        assert npsso == "the-npsso-token"
        return FakeAgent(sub_arg, npsso, email_info=("user@example.com", True))

    result = await link(
        sub,
        "the-npsso-token",
        "User@Example.com",
        repository=repo,
        token_crypto=crypto,
        agent_factory=agent_factory,
    )

    assert isinstance(result, LinkResult)
    assert result.psn_account_id == "psn-account-1"
    assert repo.set_link_account_calls == [(sub, "psn-account-1")]
    assert repo.delete_calls == []
    assert repo.links[sub].psn_account_id == "psn-account-1"
    assert repo.touch_verified_calls == [sub]
    assert repo.links[sub].last_verified_at is not None


async def test_link_address_mismatch_clears_and_raises():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = "sub-1"
    await _seed_link(repo, crypto, sub)

    async def agent_factory(sub_arg, npsso=None):
        return FakeAgent(sub_arg, npsso, email_info=("other@example.com", True))

    with pytest.raises(LinkError) as exc_info:
        await link(
            sub,
            "npsso",
            "user@example.com",
            repository=repo,
            token_crypto=crypto,
            agent_factory=agent_factory,
        )

    assert exc_info.value.kind == "mismatch"
    assert repo.delete_calls == [sub]
    assert repo.set_link_account_calls == []
    assert repo.touch_verified_calls == []
    assert sub not in repo.links


async def test_link_matching_but_unverified_clears_and_raises():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = "sub-1"
    await _seed_link(repo, crypto, sub)

    async def agent_factory(sub_arg, npsso=None):
        return FakeAgent(sub_arg, npsso, email_info=("user@example.com", False))

    with pytest.raises(LinkError) as exc_info:
        await link(
            sub,
            "npsso",
            "user@example.com",
            repository=repo,
            token_crypto=crypto,
            agent_factory=agent_factory,
        )

    assert exc_info.value.kind == "unverified"
    assert repo.delete_calls == [sub]
    assert repo.set_link_account_calls == []
    assert repo.touch_verified_calls == []


async def test_link_none_email_clears_and_raises_unverified():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = "sub-1"
    await _seed_link(repo, crypto, sub)

    async def agent_factory(sub_arg, npsso=None):
        return FakeAgent(sub_arg, npsso, email_info=None)

    with pytest.raises(LinkError) as exc_info:
        await link(
            sub,
            "npsso",
            "user@example.com",
            repository=repo,
            token_crypto=crypto,
            agent_factory=agent_factory,
        )

    assert exc_info.value.kind == "unverified"
    assert repo.delete_calls == [sub]
    assert repo.set_link_account_calls == []
    assert repo.touch_verified_calls == []


async def test_link_psn_auth_error_clears_and_raises_auth_failed():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = "sub-1"
    await _seed_link(repo, crypto, sub)

    async def agent_factory(sub_arg, npsso=None):
        return FakeAgent(sub_arg, npsso, raise_on=("whoami",))

    with pytest.raises(LinkError) as exc_info:
        await link(
            sub,
            "npsso",
            "user@example.com",
            repository=repo,
            token_crypto=crypto,
            agent_factory=agent_factory,
        )

    assert exc_info.value.kind == "auth_failed"
    assert repo.delete_calls == [sub]
    assert repo.set_link_account_calls == []
    assert repo.touch_verified_calls == []


async def test_link_verified_match_with_no_refresh_token_still_links():
    """PSN's token response omits refresh_token for some auth modes (e.g. passkey sign-in). whoami()'s
    bootstrap now persists that access-token-only session anyway (DbTokenStore.save() only requires
    access_token), so a verified email match must still complete the link -- it's usable until
    access_token_expires_at, after which reverify_link() will clear it and prompt for a fresh npsso.
    """
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = "sub-1"
    encrypted = crypto.encrypt(b'{"access_token": "AT"}')
    await repo.upsert_link(
        sub,
        encrypted,
        datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        None,
    )

    async def agent_factory(sub_arg, npsso=None):
        return FakeAgent(sub_arg, npsso, email_info=("user@example.com", True))

    result = await link(
        sub,
        "npsso",
        "user@example.com",
        repository=repo,
        token_crypto=crypto,
        agent_factory=agent_factory,
    )

    assert isinstance(result, LinkResult)
    assert result.psn_account_id == "psn-account-1"
    assert result.access_token_expires_at is not None
    assert result.refresh_token_expires_at is None
    assert repo.set_link_account_calls == [(sub, "psn-account-1")]
    assert repo.touch_verified_calls == [sub]
    assert repo.links[sub].psn_account_id == "psn-account-1"


async def test_link_invalid_npsso_rejected_before_any_agent_call():
    repo = FakeRepository()
    crypto = _make_crypto()
    calls: list[str] = []

    async def agent_factory(sub_arg, npsso=None):
        calls.append(sub_arg)
        raise AssertionError("agent_factory must not be called for an invalid npsso")

    with pytest.raises(LinkError) as exc_info:
        await link(
            "sub-1",
            "{not valid json",
            "user@example.com",
            repository=repo,
            token_crypto=crypto,
            agent_factory=agent_factory,
        )

    assert exc_info.value.kind == "invalid_npsso"
    assert calls == []
    assert repo.set_link_account_calls == []
    assert repo.delete_calls == []


async def test_link_accepts_npsso_json_blob():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = "sub-1"
    await _seed_link(repo, crypto, sub)

    async def agent_factory(sub_arg, npsso=None):
        assert npsso == '{"npsso": "abc123"}'
        return FakeAgent(sub_arg, npsso, email_info=("user@example.com", True))

    result = await link(
        sub,
        '{"npsso": "abc123"}',
        "user@example.com",
        repository=repo,
        token_crypto=crypto,
        agent_factory=agent_factory,
    )
    assert result.psn_account_id == "psn-account-1"


async def test_unlink_clears_the_link():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = "sub-1"
    await _seed_link(repo, crypto, sub)

    await unlink(sub, repository=repo, token_crypto=crypto)

    assert repo.delete_calls == [sub]
    assert sub not in repo.links
