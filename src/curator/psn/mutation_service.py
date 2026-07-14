"""Mutating PSN social/chat operations, gated by the mutation-safety wall (:mod:`curator.psn.safety`).

Ported from ``psnpy.client.PsnAgent``'s mutating methods. Per the migration plan: fully ported and tested,
but **no API routes are wired up to these yet** -- they're outside curation scope. Everything here is ready
to wire up whenever a real feature needs it, so nothing from the ``psnpy`` fold-in is lost.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from curator.psn import _identity
from curator.psn.account_client import AccountClient
from curator.psn.models import SentMessage
from curator.psn.safety import MutationGuard
from curator.psn.session import PsnSession

_GAMING_LOUNGE_URI = "https://m.np.playstation.com/api/gamingLoungeGroups/v1"
_PROFILE_URI = "https://m.np.playstation.com/api/userProfile/v1/internal/users"


def _epoch_millis_iso(value: Any) -> str | None:
    """Convert a PSN epoch-milliseconds timestamp (string or int) to an ISO-8601 UTC string."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class MutationService:
    """Test-account-walled PSN mutations: chat groups/messages, friend requests.

    Every method calls :meth:`~curator.psn.safety.MutationGuard.require_pinned` first, live, before doing
    anything else -- the check is on the immutable ``account_id`` and runs fresh on every call, so no
    cached token can bypass it.

    :param session: The authenticated session to call through.
    :param guard: The mutation-safety wall for the calling user.
    """

    def __init__(self, session: PsnSession, guard: MutationGuard) -> None:
        self._session = session
        self._guard = guard
        self._account_client = AccountClient(session)

    async def _require_test_account(self) -> None:
        live_account = await self._account_client.whoami()
        await self._guard.require_pinned(live_account)

    async def _resolve_account_ids(self, online_ids: list[str] | None, account_ids: list[str] | None) -> list[str]:
        resolved = [await _identity.account_id_for(self._session, online_id, None) for online_id in (online_ids or [])]
        resolved += list(account_ids or [])
        return resolved

    async def create_group(
        self,
        online_ids: list[str] | None = None,
        account_ids: list[str] | None = None,
    ) -> str | None:
        """Create a new chat group (group DM) with the given members. Test account only.

        :param online_ids: Member online ids to add.
        :param account_ids: Member account ids to add.
        :returns: The new group's id.
        """
        await self._require_test_account()
        members = await self._resolve_account_ids(online_ids, account_ids)
        response = (
            await self._session.post(
                f"{_GAMING_LOUNGE_URI}/groups",
                json={"invitees": [{"accountId": account_id} for account_id in members]},
            )
        ).json()
        group_id = response.get("groupId")
        return str(group_id) if group_id is not None else None

    async def rename_group(self, group_id: str, name: str) -> None:
        """Rename a chat group. Test account only.

        :param group_id: The group's id.
        :param name: The new group name.
        """
        await self._require_test_account()
        await self._session.patch(f"{_GAMING_LOUNGE_URI}/groups/{group_id}", json={"groupName": {"value": name}})

    async def send_message(self, group_id: str, text: str) -> SentMessage:
        """Send a text message to a chat group. Test account only.

        :param group_id: The group's id.
        :param text: The message body.
        :returns: The :class:`~curator.psn.models.SentMessage` (id + created timestamp).
        """
        await self._require_test_account()
        data = (
            await self._session.post(
                f"{_GAMING_LOUNGE_URI}/groups/{group_id}/threads/{group_id}/messages",
                json={"messageType": 1, "body": text},
            )
        ).json()
        return SentMessage(
            message_uid=data.get("messageUid"), created_at=_epoch_millis_iso(data.get("createdTimestamp"))
        )

    async def invite_to_group(
        self,
        group_id: str,
        online_ids: list[str] | None = None,
        account_ids: list[str] | None = None,
    ) -> None:
        """Invite one or more users to a chat group. Test account only.

        :param group_id: The group's id.
        :param online_ids: Invitee online ids.
        :param account_ids: Invitee account ids.
        """
        await self._require_test_account()
        members = await self._resolve_account_ids(online_ids, account_ids)
        await self._session.post(
            f"{_GAMING_LOUNGE_URI}/groups",
            json={"invitees": [{"accountId": account_id} for account_id in members]},
        )

    async def kick_from_group(
        self,
        group_id: str,
        online_id: str | None = None,
        account_id: str | None = None,
    ) -> None:
        """Remove a member from a chat group. Test account only. Destructive.

        :param group_id: The group's id.
        :param online_id: The member's online id.
        :param account_id: The member's account id.
        """
        await self._require_test_account()
        target_account_id = await _identity.account_id_for(self._session, online_id, account_id)
        await self._session.delete(f"{_GAMING_LOUNGE_URI}/groups/{group_id}/members/{target_account_id}")

    async def leave_group(self, group_id: str) -> None:
        """Leave a chat group. Test account only. Destructive.

        :param group_id: The group's id.
        """
        await self._require_test_account()
        await self._session.delete(f"{_GAMING_LOUNGE_URI}/groups/{group_id}/members/me")

    async def accept_friend(self, online_id: str | None = None, account_id: str | None = None) -> None:
        """Accept a friend request from (or send one to) a user. Test account only.

        PSN uses the same call to send a request and to accept one.

        :param online_id: The other user's online id.
        :param account_id: The other user's account id.
        """
        await self._require_test_account()
        target_account_id = await _identity.account_id_for(self._session, online_id, account_id)
        await self._session.put(f"{_PROFILE_URI}/me/friends/{target_account_id}")

    async def remove_friend(self, online_id: str | None = None, account_id: str | None = None) -> None:
        """Remove a friend, or decline a pending friend request. Test account only. Destructive.

        :param online_id: The other user's online id.
        :param account_id: The other user's account id.
        """
        await self._require_test_account()
        target_account_id = await _identity.account_id_for(self._session, online_id, account_id)
        await self._session.delete(f"{_PROFILE_URI}/me/friends/{target_account_id}")
