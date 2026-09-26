"""Tests for the mutation-safety wall (MutationGuard), using a hand-written fake repository.

Ported from ``psnpy``'s ``test_mutations.py``, now exercising the DB-backed ``psn_test_accounts`` shape
via a fake repository instead of a temp file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from curator.psn.account_client import Account
from curator.psn.errors import MutationNotAllowedError
from curator.psn.safety import (
    CHAT_WRITES,
    DEFAULT_TEST_ONLINE_ID,
    FRIEND_WRITES,
    LEGACY_TEST_ONLINE_ID_ENV_NAME,
    MUTATION_DAILY_CAP,
    TEST_ONLINE_ID_ENV_NAME,
    MutationGuard,
    expected_test_online_id,
)
from test_values import lowercase_token, new_account_id, new_identity_sub, new_online_id


class FakePinnedAccountRepository:
    def __init__(self):
        self.pinned: dict[str, str] = {}
        self.pin_calls: list[tuple[str, str]] = []

    async def get_pinned_account_id(self, identity_sub):
        return self.pinned.get(identity_sub)

    async def pin(self, identity_sub, psn_account_id):
        self.pin_calls.append((identity_sub, psn_account_id))
        self.pinned[identity_sub] = psn_account_id


class FakeLink:
    def __init__(self, psn_account_id, *, allow_friend_writes=False, allow_chat_writes=False):
        self.psn_account_id = psn_account_id
        self.allow_friend_writes = allow_friend_writes
        self.allow_chat_writes = allow_chat_writes


class FakeLinkReader:
    def __init__(self, link=None):
        self.link = link

    async def get_link(self, sub):
        return self.link


class FakeMutationCounter:
    def __init__(self, spent=0):
        self.spent = spent
        self.calls: list[tuple[str, tuple[str, ...], datetime]] = []

    async def count_since(self, identity_sub, actions, since):
        self.calls.append((identity_sub, actions, since))
        return self.spent


def _account(account_id=None):
    return Account(account_id=account_id or new_account_id(), online_id=new_online_id())


def _consenting_guard(linked_account, *, spent=0, **flags):
    counter = FakeMutationCounter(spent=spent)
    guard = MutationGuard(
        new_identity_sub(),
        FakePinnedAccountRepository(),
        links=FakeLinkReader(FakeLink(linked_account.account_id, **flags)),
        mutations=counter,
    )
    return guard, counter


def test_expected_test_online_id_defaults_when_no_env_var_set(monkeypatch):
    monkeypatch.delenv(TEST_ONLINE_ID_ENV_NAME, raising=False)
    monkeypatch.delenv(LEGACY_TEST_ONLINE_ID_ENV_NAME, raising=False)

    assert expected_test_online_id() == DEFAULT_TEST_ONLINE_ID


def test_expected_test_online_id_reads_curator_env_var(monkeypatch):
    online_id = new_online_id()
    monkeypatch.setenv(TEST_ONLINE_ID_ENV_NAME, online_id)

    assert expected_test_online_id() == online_id


def test_expected_test_online_id_falls_back_to_legacy_psnpy_env_var(monkeypatch):
    online_id = new_online_id()
    monkeypatch.delenv(TEST_ONLINE_ID_ENV_NAME, raising=False)
    monkeypatch.setenv(LEGACY_TEST_ONLINE_ID_ENV_NAME, online_id)

    assert expected_test_online_id() == online_id


async def test_register_pins_matching_account(monkeypatch):
    identity_sub = new_identity_sub()
    account = _account()
    monkeypatch.setenv(TEST_ONLINE_ID_ENV_NAME, account.online_id)
    repo = FakePinnedAccountRepository()

    await MutationGuard(identity_sub, repo).register(account)

    assert repo.pin_calls == [(identity_sub, account.account_id)]


async def test_register_rejects_non_matching_account(monkeypatch):
    monkeypatch.setenv(TEST_ONLINE_ID_ENV_NAME, new_online_id())
    repo = FakePinnedAccountRepository()

    with pytest.raises(MutationNotAllowedError, match="not the expected test account"):
        await MutationGuard(new_identity_sub(), repo).register(_account())

    assert repo.pin_calls == []


async def test_require_pinned_raises_when_nothing_pinned():
    guard = MutationGuard(new_identity_sub(), FakePinnedAccountRepository())

    with pytest.raises(MutationNotAllowedError, match="No test account is registered"):
        await guard.require_pinned(_account())


async def test_require_pinned_raises_when_live_account_differs():
    identity_sub = new_identity_sub()
    repo = FakePinnedAccountRepository()
    repo.pinned[identity_sub] = new_account_id()
    guard = MutationGuard(identity_sub, repo)

    with pytest.raises(MutationNotAllowedError, match="Refusing to perform a mutating action"):
        await guard.require_pinned(_account())


async def test_require_pinned_succeeds_when_live_account_matches():
    identity_sub = new_identity_sub()
    pinned_account = _account()
    repo = FakePinnedAccountRepository()
    repo.pinned[identity_sub] = pinned_account.account_id

    await MutationGuard(identity_sub, repo).require_pinned(pinned_account)


async def test_pinned_state_is_per_user():
    pinning_sub, other_sub = new_identity_sub(), new_identity_sub()
    pinned_account = _account()
    repo = FakePinnedAccountRepository()
    repo.pinned[pinning_sub] = pinned_account.account_id

    await MutationGuard(pinning_sub, repo).require_pinned(pinned_account)

    with pytest.raises(MutationNotAllowedError):
        await MutationGuard(other_sub, repo).require_pinned(pinned_account)


async def test_require_allowed_raises_when_no_link_store_is_configured():
    guard = MutationGuard(new_identity_sub(), FakePinnedAccountRepository())

    with pytest.raises(MutationNotAllowedError, match="No PSN link store is configured"):
        await guard.require_allowed(_account(), FRIEND_WRITES)


async def test_require_allowed_raises_when_user_has_no_link():
    guard = MutationGuard(new_identity_sub(), FakePinnedAccountRepository(), links=FakeLinkReader(None))

    with pytest.raises(MutationNotAllowedError, match="No PSN account is linked"):
        await guard.require_allowed(_account(), FRIEND_WRITES)


async def test_require_allowed_raises_when_live_account_is_not_the_linked_one():
    guard, _ = _consenting_guard(_account(), allow_friend_writes=True)

    with pytest.raises(MutationNotAllowedError, match=r"not the .*linked"):
        await guard.require_allowed(_account(), FRIEND_WRITES)


async def test_require_allowed_raises_when_capability_is_not_consented():
    linked_account = _account()
    guard, _ = _consenting_guard(linked_account, allow_friend_writes=False)

    with pytest.raises(MutationNotAllowedError, match=FRIEND_WRITES):
        await guard.require_allowed(linked_account, FRIEND_WRITES)


async def test_require_allowed_does_not_let_one_capability_authorize_the_other():
    linked_account = _account()
    guard, _ = _consenting_guard(linked_account, allow_friend_writes=True, allow_chat_writes=False)

    with pytest.raises(MutationNotAllowedError, match=CHAT_WRITES):
        await guard.require_allowed(linked_account, CHAT_WRITES)


async def test_require_allowed_succeeds_for_linked_and_consented_account():
    linked_account = _account()
    guard, counter = _consenting_guard(linked_account, allow_chat_writes=True)

    await guard.require_allowed(linked_account, CHAT_WRITES)

    assert counter.calls


async def test_require_allowed_counts_mutations_over_a_rolling_24_hours():
    linked_account = _account()
    guard, counter = _consenting_guard(linked_account, allow_chat_writes=True)

    await guard.require_allowed(linked_account, CHAT_WRITES)

    _, _, since = counter.calls[0]
    assert abs((datetime.now(timezone.utc) - timedelta(days=1)) - since) < timedelta(seconds=5)


async def test_require_allowed_raises_when_this_attempt_would_exceed_the_daily_cap():
    attempts_including_this_one = MUTATION_DAILY_CAP + 1
    linked_account = _account()
    guard, _ = _consenting_guard(linked_account, spent=attempts_including_this_one, allow_chat_writes=True)

    with pytest.raises(MutationNotAllowedError, match="Daily PSN change limit reached"):
        await guard.require_allowed(linked_account, CHAT_WRITES)


async def test_require_allowed_permits_the_attempt_that_reaches_the_cap_exactly():
    attempts_including_this_one = MUTATION_DAILY_CAP
    linked_account = _account()
    guard, _ = _consenting_guard(linked_account, spent=attempts_including_this_one, allow_chat_writes=True)

    await guard.require_allowed(linked_account, CHAT_WRITES)


async def test_require_allowed_skips_the_cap_when_no_counter_is_configured():
    linked_account = _account()
    guard = MutationGuard(
        new_identity_sub(),
        FakePinnedAccountRepository(),
        links=FakeLinkReader(FakeLink(linked_account.account_id, allow_chat_writes=True)),
    )

    await guard.require_allowed(linked_account, CHAT_WRITES)


async def test_require_allowed_rejects_an_unknown_capability():
    linked_account = _account()
    guard, _ = _consenting_guard(linked_account, allow_chat_writes=True)

    with pytest.raises(AssertionError):
        await guard.require_allowed(linked_account, lowercase_token())
