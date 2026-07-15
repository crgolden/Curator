"""Tests for reverify_link: hand-written fake Repository + fake PSN agent, real TokenCrypto."""

from __future__ import annotations

from datetime import datetime, timezone

from cryptography.fernet import Fernet

from curator.persistence.crypto import TokenCrypto
from curator.persistence.repository import LinkRecord
from curator.psn.errors import PsnAuthError
from curator.reverify import reverify_link
from curator.token_validation import TokenClaims


class FakeRepository:
    """Stands in for Repository: in-memory dict of sub -> LinkRecord, with call recording."""

    def __init__(self) -> None:
        self.links: dict[str, LinkRecord] = {}
        self.delete_calls: list[str] = []
        self.touch_verified_calls: list[str] = []

    async def get_link(self, sub):
        return self.links.get(sub)

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


class FakeAgent:
    """A fake PSN agent whose account_email_verified() either returns a canned value or raises."""

    def __init__(self, sub, npsso=None, *, email_info=None, raise_error=None):
        self.sub = sub
        self.npsso = npsso
        self._email_info = email_info
        self._raise_error = raise_error

    async def account_email_verified(self):
        if self._raise_error is not None:
            raise self._raise_error
        return self._email_info


def _make_crypto() -> TokenCrypto:
    return TokenCrypto(Fernet.generate_key())


def _seed_access_token_only_link(repo: FakeRepository, crypto: TokenCrypto, sub: str) -> None:
    """Simulate a persisted link whose token response has no refresh_token (e.g. passkey sign-in)."""
    encrypted = crypto.encrypt(b'{"access_token": "AT"}')
    repo.links[sub] = LinkRecord(
        psn_account_id="psn-account-1",
        token_response_enc=encrypted,
        access_token_expires_at=datetime(2026, 1, 1, 1, tzinfo=timezone.utc),
        refresh_token_expires_at=None,
        linked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_verified_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


async def test_reverify_clears_access_token_only_link_when_expired_access_token_raises_auth_error():
    """Once an access-token-only session's access token expires, PsnSession._refresh() has no refresh_token
    to use and raises PsnAuthError. reverify_link() must treat that exactly like any other PSN auth failure:
    clear the stale link so the user is prompted for a fresh npsso.
    """
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = "sub-1"
    _seed_access_token_only_link(repo, crypto, sub)
    claims = TokenClaims(
        sub=sub, email="user@example.com", iat=datetime(2026, 2, 1, tzinfo=timezone.utc), scopes=("curator",)
    )

    async def agent_factory(sub_arg, npsso=None):
        return FakeAgent(sub_arg, npsso, raise_error=PsnAuthError("no refresh token available"))

    await reverify_link(claims, repository=repo, token_crypto=crypto, agent_factory=agent_factory)

    assert repo.delete_calls == [sub]
    assert sub not in repo.links
    assert repo.touch_verified_calls == []
