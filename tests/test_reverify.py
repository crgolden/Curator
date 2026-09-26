"""Tests for reverify_link: hand-written fake Repository + fake PSN agent, real TokenCrypto."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from audit_fakes import RecordingAuditRepository
from curator.audit.repository import ACTION_LINK_REVERIFIED, OUTCOME_COMPLETED, OUTCOME_FAILED, OUTCOME_STARTED
from curator.deps import CURATOR_SCOPE
from curator.persistence.crypto import TokenCrypto
from curator.persistence.repository import LinkRecord
from curator.psn.errors import PsnAuthError
from curator.reverify import REVERIFY_AUTH_FAILED, REVERIFY_PSN_UNAVAILABLE, reverify_link
from curator.token_validation import TokenClaims
from test_values import new_email_address, new_identity_sub

EMAIL = new_email_address()


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
    return TokenCrypto(TokenCrypto.generate_key())


def _seed_access_token_only_link(repo: FakeRepository, crypto: TokenCrypto, sub: str) -> None:
    """Simulate a persisted link whose token response has no refresh_token (a theoretical edge case now that
    _authorization_code() requests access_type=offline -- see reverify_link()'s docstring)."""
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
    claims = TokenClaims(sub=sub, email=EMAIL, iat=datetime(2026, 2, 1, tzinfo=timezone.utc), scopes=(CURATOR_SCOPE,))

    async def agent_factory(sub_arg, npsso=None):
        return FakeAgent(sub_arg, npsso, raise_error=PsnAuthError("no refresh token available"))

    recorder = RecordingAuditRepository()
    await reverify_link(claims, repository=repo, token_crypto=crypto, agent_factory=agent_factory, recorder=recorder)

    assert repo.delete_calls == [sub]
    assert sub not in repo.links
    assert repo.touch_verified_calls == []
    assert [(row.action, row.detail, row.outcome) for row in recorder.rows] == [
        (ACTION_LINK_REVERIFIED, REVERIFY_AUTH_FAILED, OUTCOME_FAILED)
    ]


def _stale_verification_claims(sub: str) -> TokenClaims:
    return TokenClaims(sub=sub, email=EMAIL, iat=datetime(2026, 2, 1, tzinfo=timezone.utc), scopes=(CURATOR_SCOPE,))


async def test_reverify_records_the_check_before_it_reaches_psn():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = new_identity_sub()
    _seed_access_token_only_link(repo, crypto, sub)
    recorder = RecordingAuditRepository()
    history_when_psn_was_asked: list[list[tuple[str, str]]] = []

    async def agent_factory(sub_arg, npsso=None):
        history_when_psn_was_asked.append(recorder.outcomes)
        return FakeAgent(sub_arg, npsso, email_info=(EMAIL, True))

    await reverify_link(
        _stale_verification_claims(sub),
        repository=repo,
        token_crypto=crypto,
        agent_factory=agent_factory,
        recorder=recorder,
    )

    assert history_when_psn_was_asked == [[(ACTION_LINK_REVERIFIED, OUTCOME_STARTED)]]
    assert recorder.outcomes == [(ACTION_LINK_REVERIFIED, OUTCOME_COMPLETED)]


async def test_reverify_never_asks_psn_when_the_history_row_cannot_be_written():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = new_identity_sub()
    _seed_access_token_only_link(repo, crypto, sub)
    recorder = RecordingAuditRepository()
    recorder.begin_error = RuntimeError(sub)
    asked: list[str] = []

    async def agent_factory(sub_arg, npsso=None):
        asked.append(sub_arg)
        return FakeAgent(sub_arg, npsso, email_info=(EMAIL, True))

    with pytest.raises(RuntimeError):
        await reverify_link(
            _stale_verification_claims(sub),
            repository=repo,
            token_crypto=crypto,
            agent_factory=agent_factory,
            recorder=recorder,
        )

    assert asked == []


async def test_reverify_records_an_unreachable_psn_as_failed_and_keeps_the_link():
    repo = FakeRepository()
    crypto = _make_crypto()
    sub = new_identity_sub()
    _seed_access_token_only_link(repo, crypto, sub)
    recorder = RecordingAuditRepository()

    async def agent_factory(sub_arg, npsso=None):
        return FakeAgent(sub_arg, npsso, raise_error=ConnectionError(sub_arg))

    await reverify_link(
        _stale_verification_claims(sub),
        repository=repo,
        token_crypto=crypto,
        agent_factory=agent_factory,
        recorder=recorder,
    )

    assert sub in repo.links
    assert [(row.action, row.detail, row.outcome) for row in recorder.rows] == [
        (ACTION_LINK_REVERIFIED, REVERIFY_PSN_UNAVAILABLE, OUTCOME_FAILED)
    ]
