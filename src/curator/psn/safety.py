"""The mutation safety wall for state-changing PSN calls.

Read-only calls can target any account, but **mutations** (creating groups, sending messages, kicking
members, accepting/removing friends, ...) change state on a real account, and several are visible to third
parties who are not Curator users. :class:`MutationGuard` re-checks the live-authenticated account on every
mutation against the account currently linked to that Curator user, and against the consent flag for the
operation's class. Because the check is on the immutable ``account_id`` and runs live, no cached or stolen
token can widen its own permissions.

The pinned-account store is DB-backed (:class:`~curator.psn.repository.PinnedAccountRepository`,
``psn_test_accounts``) rather than a local file, since Curator is a multi-instance web service where a
local file can't be trusted to persist or be visible across instances.
:meth:`MutationGuard.register`/:meth:`MutationGuard.require_pinned` and ``psn_test_accounts`` exist for
development and manual testing; they are not the production path.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Protocol

from curator.psn.errors import MutationNotAllowedError

if TYPE_CHECKING:
    from curator.persistence.repository import LinkRecord
    from curator.psn.account_client import Account

DEFAULT_TEST_ONLINE_ID = "curator-test-account"
_TEST_ONLINE_ID_ENV_NAMES: tuple[str, ...] = ("CURATOR_PSN_TEST_ONLINE_ID", "PSNPY_TEST_ONLINE_ID")

FRIEND_WRITES = "allow_friend_writes"
CHAT_WRITES = "allow_chat_writes"

MUTATION_DAILY_CAP = 50


def expected_test_online_id() -> str:
    """Return the online id the test account must have, from an env var or the default."""
    for env_name in _TEST_ONLINE_ID_ENV_NAMES:
        value = os.environ.get(env_name)
        if value:
            return value
    return DEFAULT_TEST_ONLINE_ID


class PinnedAccountRepository(Protocol):
    """Duck-typed async pinned-test-account store.

    Satisfied by :class:`curator.psn.repository.PinnedAccountRepository`.
    """

    async def get_pinned_account_id(self, identity_sub: str) -> str | None:
        """Return the pinned test account's ``account_id`` for this user, or ``None`` if none is pinned."""
        ...

    async def pin(self, identity_sub: str, psn_account_id: str) -> None:
        """Pin ``psn_account_id`` as this user's test account, replacing any previous pin."""
        ...


class LinkReader(Protocol):
    """The slice of :class:`curator.persistence.repository.Repository` the guard needs."""

    async def get_link(self, sub: str) -> LinkRecord | None:
        """Return the user's stored PSN link, or ``None`` if they have none."""
        ...


class MutationCounter(Protocol):
    """The slice of :class:`curator.audit.repository.AccountActionLogRepository` the cap needs."""

    async def count_since(self, identity_sub: str, actions: tuple[str, ...], since: datetime) -> int:
        """Count this user's logged ``actions`` at or after ``since``."""
        ...


class MutationGuard:
    """Confines mutating PSN operations to the account a Curator user has linked, and to the operation
    classes they have consented to.

    :param identity_sub: The Curator user id (Identity's ``sub``) performing the operation.
    :param repository: The pinned-test-account store, for the development-only pin.
    :param links: The PSN link store, for the production predicate.
    :param mutations: The audit log, which doubles as the mutation counter for the daily cap.
    """

    def __init__(
        self,
        identity_sub: str,
        repository: PinnedAccountRepository,
        links: LinkReader | None = None,
        mutations: MutationCounter | None = None,
    ) -> None:
        self._identity_sub = identity_sub
        self._repository = repository
        self._links = links
        self._mutations = mutations

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

    async def require_allowed(self, live_account: Account, capability: str) -> None:
        """Re-check a live-authenticated account against this user's link and consent for ``capability``.

        :param live_account: The live-authenticated account (from
            :meth:`~curator.psn.account_client.AccountClient.whoami`).
        :param capability: :data:`FRIEND_WRITES` or :data:`CHAT_WRITES`.
        :raises MutationNotAllowedError: If the user has no link, the live account is not the linked one,
            the capability is not enabled, or the daily mutation cap is already spent.
        """
        assert capability in (FRIEND_WRITES, CHAT_WRITES), f"unknown capability: {capability!r}"
        if self._links is None:
            raise MutationNotAllowedError("No PSN link store is configured; refusing to mutate.")

        link = await self._links.get_link(self._identity_sub)
        if link is None:
            raise MutationNotAllowedError("No PSN account is linked; link one before making changes on PSN.")
        if link.psn_account_id is None or live_account.account_id != link.psn_account_id:
            raise MutationNotAllowedError(
                f"Refusing to perform a mutating action as '{live_account.online_id}'. It is not the "
                "PSN account currently linked to this user."
            )
        if getattr(link, capability) is not True:
            raise MutationNotAllowedError(f"PSN capability '{capability}' is not enabled for this user.")

        await self._require_cap_remaining()

    async def _require_cap_remaining(self) -> None:
        if self._mutations is None:
            return
        from curator.audit.repository import PSN_MUTATION_ACTIONS

        since = datetime.now(timezone.utc) - timedelta(days=1)
        spent = await self._mutations.count_since(self._identity_sub, PSN_MUTATION_ACTIONS, since)
        if spent >= MUTATION_DAILY_CAP:
            raise MutationNotAllowedError(
                f"Daily PSN change limit reached ({MUTATION_DAILY_CAP} in 24 hours). Try again later."
            )
