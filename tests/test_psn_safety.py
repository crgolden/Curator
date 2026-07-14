"""Tests for the mutation-safety wall (MutationGuard), using a hand-written fake repository.

Ported from ``psnpy``'s ``test_mutations.py``, now exercising the DB-backed ``psn_test_accounts`` shape
via a fake repository instead of a temp file.
"""

from __future__ import annotations

import os

import pytest

from curator.psn.account_client import Account
from curator.psn.errors import MutationNotAllowedError
from curator.psn.safety import DEFAULT_TEST_ONLINE_ID, MutationGuard, expected_test_online_id


class FakeTestAccountRepository:
    def __init__(self):
        self.pinned: dict[str, str] = {}
        self.pin_calls: list[tuple[str, str]] = []

    async def get_pinned_account_id(self, identity_sub):
        return self.pinned.get(identity_sub)

    async def pin(self, identity_sub, psn_account_id):
        self.pin_calls.append((identity_sub, psn_account_id))
        self.pinned[identity_sub] = psn_account_id


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
    repo = FakeTestAccountRepository()
    guard = MutationGuard("sub-1", repo)
    account = Account(account_id="acct-1", online_id="curator-test-account")

    await guard.register(account)

    assert repo.pin_calls == [("sub-1", "acct-1")]


async def test_register_rejects_non_matching_account(monkeypatch):
    monkeypatch.setenv("CURATOR_PSN_TEST_ONLINE_ID", "curator-test-account")
    repo = FakeTestAccountRepository()
    guard = MutationGuard("sub-1", repo)
    account = Account(account_id="acct-1", online_id="wrong-account")

    with pytest.raises(MutationNotAllowedError, match="not the expected test account"):
        await guard.register(account)

    assert repo.pin_calls == []


async def test_require_pinned_raises_when_nothing_pinned():
    guard = MutationGuard("sub-1", FakeTestAccountRepository())
    account = Account(account_id="acct-1", online_id="whoever")

    with pytest.raises(MutationNotAllowedError, match="No test account is registered"):
        await guard.require_pinned(account)


async def test_require_pinned_raises_when_live_account_differs():
    repo = FakeTestAccountRepository()
    repo.pinned["sub-1"] = "acct-pinned"
    guard = MutationGuard("sub-1", repo)
    account = Account(account_id="acct-different", online_id="someone-else")

    with pytest.raises(MutationNotAllowedError, match="Refusing to perform a mutating action"):
        await guard.require_pinned(account)


async def test_require_pinned_succeeds_when_live_account_matches():
    repo = FakeTestAccountRepository()
    repo.pinned["sub-1"] = "acct-pinned"
    guard = MutationGuard("sub-1", repo)
    account = Account(account_id="acct-pinned", online_id="curator-test-account")

    await guard.require_pinned(account)  # does not raise


async def test_pinned_state_is_per_user():
    repo = FakeTestAccountRepository()
    repo.pinned["sub-a"] = "acct-a"
    guard_a = MutationGuard("sub-a", repo)
    guard_b = MutationGuard("sub-b", repo)

    await guard_a.require_pinned(Account(account_id="acct-a", online_id="a"))

    with pytest.raises(MutationNotAllowedError):
        await guard_b.require_pinned(Account(account_id="acct-a", online_id="a"))
