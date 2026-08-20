"""Tests for the mutation-safety wall (MutationGuard), using a hand-written fake repository.

Ported from ``psnpy``'s ``test_mutations.py``, now exercising the DB-backed ``psn_test_accounts`` shape
via a fake repository instead of a temp file.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from curator.psn.account_client import Account
from curator.psn.errors import MutationNotAllowedError
from curator.psn.safety import (
    CHAT_WRITES,
    DEFAULT_TEST_ONLINE_ID,
    FRIEND_WRITES,
    MUTATION_DAILY_CAP,
    MutationGuard,
    expected_test_online_id,
)


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


def _consenting_guard(*, spent=0, **flags):
    counter = FakeMutationCounter(spent=spent)
    guard = MutationGuard(
        "sub-1",
        FakePinnedAccountRepository(),
        links=FakeLinkReader(FakeLink("acct-linked", **flags)),
        mutations=counter,
    )
    return guard, counter


def test_expected_test_online_id_defaults_when_no_env_var_set(monkeypatch):
    monkeypatch.delenv("CURATOR_PSN_TEST_ONLINE_ID", raising=False)
    monkeypatch.delenv("PSNPY_TEST_ONLINE_ID", raising=False)

    assert expected_test_online_id() == DEFAULT_TEST_ONLINE_ID


def test_expected_test_online_id_reads_curator_env_var(monkeypatch):
    monkeypatch.setenv("CURATOR_PSN_TEST_ONLINE_ID", "my-test-account")

    assert expected_test_online_id() == "my-test-account"


def test_expected_test_online_id_falls_back_to_legacy_psnpy_env_var(monkeypatch):
    monkeypatch.delenv("CURATOR_PSN_TEST_ONLINE_ID", raising=False)
    monkeypatch.setenv("PSNPY_TEST_ONLINE_ID", "legacy-test-account")

    assert expected_test_online_id() == "legacy-test-account"
    assert os.environ["PSNPY_TEST_ONLINE_ID"] == "legacy-test-account"


async def test_register_pins_matching_account(monkeypatch):
    monkeypatch.setenv("CURATOR_PSN_TEST_ONLINE_ID", "curator-test-account")
    repo = FakePinnedAccountRepository()
    guard = MutationGuard("sub-1", repo)
    account = Account(account_id="acct-1", online_id="curator-test-account")

    await guard.register(account)

    assert repo.pin_calls == [("sub-1", "acct-1")]


async def test_register_rejects_non_matching_account(monkeypatch):
    monkeypatch.setenv("CURATOR_PSN_TEST_ONLINE_ID", "curator-test-account")
    repo = FakePinnedAccountRepository()
    guard = MutationGuard("sub-1", repo)
    account = Account(account_id="acct-1", online_id="wrong-account")

    with pytest.raises(MutationNotAllowedError, match="not the expected test account"):
        await guard.register(account)

    assert repo.pin_calls == []


async def test_require_pinned_raises_when_nothing_pinned():
    guard = MutationGuard("sub-1", FakePinnedAccountRepository())
    account = Account(account_id="acct-1", online_id="whoever")

    with pytest.raises(MutationNotAllowedError, match="No test account is registered"):
        await guard.require_pinned(account)


async def test_require_pinned_raises_when_live_account_differs():
    repo = FakePinnedAccountRepository()
    repo.pinned["sub-1"] = "acct-pinned"
    guard = MutationGuard("sub-1", repo)
    account = Account(account_id="acct-different", online_id="someone-else")

    with pytest.raises(MutationNotAllowedError, match="Refusing to perform a mutating action"):
        await guard.require_pinned(account)


async def test_require_pinned_succeeds_when_live_account_matches():
    repo = FakePinnedAccountRepository()
    repo.pinned["sub-1"] = "acct-pinned"
    guard = MutationGuard("sub-1", repo)
    account = Account(account_id="acct-pinned", online_id="curator-test-account")

    await guard.require_pinned(account)


async def test_pinned_state_is_per_user():
    repo = FakePinnedAccountRepository()
    repo.pinned["sub-a"] = "acct-a"
    guard_a = MutationGuard("sub-a", repo)
    guard_b = MutationGuard("sub-b", repo)

    await guard_a.require_pinned(Account(account_id="acct-a", online_id="a"))

    with pytest.raises(MutationNotAllowedError):
        await guard_b.require_pinned(Account(account_id="acct-a", online_id="a"))


async def test_require_allowed_raises_when_no_link_store_is_configured():
    guard = MutationGuard("sub-1", FakePinnedAccountRepository())

    with pytest.raises(MutationNotAllowedError, match="No PSN link store is configured"):
        await guard.require_allowed(Account(account_id="acct-linked", online_id="me"), FRIEND_WRITES)


async def test_require_allowed_raises_when_user_has_no_link():
    guard = MutationGuard("sub-1", FakePinnedAccountRepository(), links=FakeLinkReader(None))

    with pytest.raises(MutationNotAllowedError, match="No PSN account is linked"):
        await guard.require_allowed(Account(account_id="acct-linked", online_id="me"), FRIEND_WRITES)


async def test_require_allowed_raises_when_live_account_is_not_the_linked_one():
    guard, _ = _consenting_guard(allow_friend_writes=True)

    with pytest.raises(MutationNotAllowedError, match=r"not the .*linked"):
        await guard.require_allowed(Account(account_id="acct-other", online_id="someone-else"), FRIEND_WRITES)


async def test_require_allowed_raises_when_capability_is_not_consented():
    guard, _ = _consenting_guard(allow_friend_writes=False)

    with pytest.raises(MutationNotAllowedError, match=FRIEND_WRITES):
        await guard.require_allowed(Account(account_id="acct-linked", online_id="me"), FRIEND_WRITES)


async def test_require_allowed_does_not_let_one_capability_authorize_the_other():
    guard, _ = _consenting_guard(allow_friend_writes=True, allow_chat_writes=False)

    with pytest.raises(MutationNotAllowedError, match=CHAT_WRITES):
        await guard.require_allowed(Account(account_id="acct-linked", online_id="me"), CHAT_WRITES)


async def test_require_allowed_succeeds_for_linked_and_consented_account():
    guard, counter = _consenting_guard(allow_chat_writes=True)

    await guard.require_allowed(Account(account_id="acct-linked", online_id="me"), CHAT_WRITES)

    assert counter.calls


async def test_require_allowed_counts_mutations_over_a_rolling_24_hours():
    guard, counter = _consenting_guard(allow_chat_writes=True)

    await guard.require_allowed(Account(account_id="acct-linked", online_id="me"), CHAT_WRITES)

    _, _, since = counter.calls[0]
    assert abs((datetime.now(timezone.utc) - timedelta(days=1)) - since) < timedelta(seconds=5)


async def test_require_allowed_raises_when_daily_cap_is_spent():
    guard, _ = _consenting_guard(spent=MUTATION_DAILY_CAP, allow_chat_writes=True)

    with pytest.raises(MutationNotAllowedError, match="Daily PSN change limit reached"):
        await guard.require_allowed(Account(account_id="acct-linked", online_id="me"), CHAT_WRITES)


async def test_require_allowed_permits_the_last_mutation_under_the_cap():
    guard, _ = _consenting_guard(spent=MUTATION_DAILY_CAP - 1, allow_chat_writes=True)

    await guard.require_allowed(Account(account_id="acct-linked", online_id="me"), CHAT_WRITES)


async def test_require_allowed_skips_the_cap_when_no_counter_is_configured():
    guard = MutationGuard(
        "sub-1",
        FakePinnedAccountRepository(),
        links=FakeLinkReader(FakeLink("acct-linked", allow_chat_writes=True)),
    )

    await guard.require_allowed(Account(account_id="acct-linked", online_id="me"), CHAT_WRITES)


async def test_require_allowed_rejects_an_unknown_capability():
    guard, _ = _consenting_guard(allow_chat_writes=True)

    with pytest.raises(AssertionError):
        await guard.require_allowed(Account(account_id="acct-linked", online_id="me"), "harvest_trophies")
