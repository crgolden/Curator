"""The mutation safety wall: confine every state-changing PSN call to a designated test account.

Read-only calls can target any account, but **mutations** (creating groups, sending messages, kicking
members, accepting/removing friends, ...) change state on a real account. To make it structurally
impossible to mutate the main account by accident, mutations are pinned to a throwaway test account: the
account's immutable ``account_id`` is captured once at registration (:class:`MutationGuard.register`) and
every mutation re-checks the live-authenticated account against it (:meth:`MutationGuard.require_pinned`).
Because the check is on the immutable id and runs live, no loaded token can bypass it.

Ported from ``psnpy.safety``, re-platformed from a local JSON file (``TestAccountStore``) to the DB-backed
:class:`~curator.psn.repository.TestAccountRepository` (``psn_test_accounts``), since Curator is a
multi-instance web service where a local file can't be trusted to persist or be visible across instances.
The pin is also now per-user (keyed by ``identity_sub``), not global to the machine.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Protocol

from curator.psn.errors import MutationNotAllowedError

if TYPE_CHECKING:
    from curator.psn.account_client import Account

# The online id the test account is expected to have. Verified once at registration so a stray npsso for
# the wrong account is never pinned; after that the wall uses the immutable account_id. This fallback is a
# placeholder only -- real usage always sets one of the env vars below to an actual test account.
DEFAULT_TEST_ONLINE_ID = "curator-test-account"
_TEST_ONLINE_ID_ENV_NAMES: tuple[str, ...] = ("CURATOR_PSN_TEST_ONLINE_ID", "PSNPY_TEST_ONLINE_ID")


def expected_test_online_id() -> str:
    """Return the online id the test account must have, from an env var or the default."""
    for env_name in _TEST_ONLINE_ID_ENV_NAMES:
        value = os.environ.get(env_name)
        if value:
            return value
    return DEFAULT_TEST_ONLINE_ID


class TestAccountRepository(Protocol):
    """Duck-typed async pinned-test-account store.

    Satisfied by :class:`curator.psn.repository.TestAccountRepository`. Never imported directly into a
    test module (tests depend on the concrete repository or their own ``FakeTestAccountRepository``), so
    unlike that concrete class this one needs no ``__test__ = False`` pytest-collection guard.
    """

    async def get_pinned_account_id(self, identity_sub: str) -> str | None:
        """Return the pinned test account's ``account_id`` for this user, or ``None`` if none is pinned."""
        ...

    async def pin(self, identity_sub: str, psn_account_id: str) -> None:
        """Pin ``psn_account_id`` as this user's test account, replacing any previous pin."""
        ...


class MutationGuard:
    """Confines mutating PSN operations to one Curator user's pinned test account.

    :param identity_sub: The Curator user id (Identity's ``sub``) performing the operation.
    :param repository: The pinned-test-account store.
    """

    def __init__(self, identity_sub: str, repository: TestAccountRepository) -> None:
        self._identity_sub = identity_sub
        self._repository = repository

    async def register(self, account: Account) -> None:
        """Verify ``account`` is the expected test account and pin it for future mutations.

        :param account: The live-authenticated account (from :meth:`~curator.psn.account_client.AccountClient.whoami`).
        :raises MutationNotAllowedError: If ``account.online_id`` doesn't match the expected test account.
        """
        expected = expected_test_online_id()
        if account.online_id != expected:
            raise MutationNotAllowedError(
                f"Authenticated as '{account.online_id}', not the expected test account '{expected}'. "
                "Supply the test account's npsso and try again. Nothing was pinned."
            )
        await self._repository.pin(self._identity_sub, account.account_id)

    async def require_pinned(self, live_account: Account) -> None:
        """Re-check a live-authenticated account against this user's pinned test account.

        :param live_account: The live-authenticated account (from
            :meth:`~curator.psn.account_client.AccountClient.whoami`).
        :raises MutationNotAllowedError: If no test account is pinned, or the live account differs.
        """
        pinned_account_id = await self._repository.get_pinned_account_id(self._identity_sub)
        if pinned_account_id is None:
            raise MutationNotAllowedError(
                "No test account is registered for this user. Mutations are confined to a pinned test "
                "account -- register one first."
            )
        if live_account.account_id != pinned_account_id:
            raise MutationNotAllowedError(
                f"Refusing to perform a mutating action as '{live_account.online_id}'. Mutations are "
                f"pinned to a different test account (id {pinned_account_id})."
            )
