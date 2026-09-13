"""Mutating PSN social/chat operations, gated by the mutation-safety wall (:mod:`curator.psn.safety`).

``send_message`` and ``kick_from_group`` are implemented and tested but deliberately unrouted -- see
``AGENTS/REPOS/Curator.md``.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from datetime import datetime, timezone
from typing import Any

from curator.psn import _identity
from curator.psn.account_client import AccountClient
from curator.psn.errors import NoPendingFriendRequestError
from curator.psn.models import SentMessage
from curator.psn.safety import CHAT_WRITES, FRIEND_WRITES, MutationGuard
from curator.psn.session import PsnSession
from curator.psn.social_client import SocialClient

_GAMING_LOUNGE_URI = "https://m.np.playstation.com/api/gamingLoungeGroups/v1"
_PROFILE_URI = "https://m.np.playstation.com/api/userProfile/v1/internal/users"

NO_FRIEND_RELATION = "no-friend"
"""PSN's ``friendRelation`` for two accounts with neither a friendship nor a pending request between them."""


def _epoch_millis_iso(value: Any) -> str | None:
    """Convert a PSN epoch-milliseconds timestamp (string or int) to an ISO-8601 UTC string."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class MutationService:
    """Consent-gated PSN mutations: chat groups/messages, friend requests.

    Every method re-checks, live, that the authenticated account is the one linked to this user and that
    the operation's capability is enabled -- the check is on the immutable ``account_id`` and runs fresh
    on every call, so no cached token can bypass it.

    :param session: The authenticated session to call through.
    :param guard: The mutation-safety wall for the calling user.
    """

    def __init__(self, session: PsnSession, guard: MutationGuard) -> None:
        self._session = session
        self._guard = guard
        self._account_client = AccountClient(session)
        self._social_client = SocialClient(session)

    async def _require(self, capability: str) -> None:
        live_account = await self._account_client.whoami()
        await self._guard.require_allowed(live_account, capability)

    async def _resolve_account_ids(self, online_ids: list[str] | None, account_ids: list[str] | None) -> list[str]:
        resolved = [await _identity.account_id_for(self._session, online_id, None) for online_id in (online_ids or [])]
        resolved += list(account_ids or [])
        return resolved

    async def create_group(
        self,
        online_ids: list[str] | None = None,
        account_ids: list[str] | None = None,
    ) -> str | None:
        """Create a new chat group (group DM) with the given members.

        :param online_ids: Member online ids to add.
        :param account_ids: Member account ids to add.
        :returns: The new group's id.
        """
        await self._require(CHAT_WRITES)
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
        """Rename a chat group.

        :param group_id: The group's id.
        :param name: The new group name.
        """
        await self._require(CHAT_WRITES)
        await self._session.patch(f"{_GAMING_LOUNGE_URI}/groups/{group_id}", json={"groupName": {"value": name}})

    async def send_message(self, group_id: str, text: str) -> SentMessage:
        """Send a text message to a chat group.

        :param group_id: The group's id.
        :param text: The message body.
        :returns: The :class:`~curator.psn.models.SentMessage` (id + created timestamp).
        """
        await self._require(CHAT_WRITES)
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
    ) -> str | None:
        """Invite one or more users to a chat group.

        PSN answers with the group the membership landed in, which is not always ``group_id``: inviting
        into a two-person DM allocates a new group holding all three members and leaves the DM as it was
        (verified live; the recording is in ``Tools/OpenAPI/PlayStation Catalog``).

        :param group_id: The group's id.
        :param online_ids: Invitee online ids.
        :param account_ids: Invitee account ids.
        :returns: The id of the group that now holds the invitees.
        """
        await self._require(CHAT_WRITES)
        members = await self._resolve_account_ids(online_ids, account_ids)
        response = (
            await self._session.post(
                f"{_GAMING_LOUNGE_URI}/groups/{group_id}/invitees",
                json={"invitees": [{"accountId": account_id} for account_id in members]},
            )
        ).json()
        resulting_group_id = response.get("groupId")
        return str(resulting_group_id) if resulting_group_id is not None else None

    async def kick_from_group(
        self,
        group_id: str,
        online_id: str | None = None,
        account_id: str | None = None,
    ) -> None:
        """Remove a member from a chat group. Destructive.

        :param group_id: The group's id.
        :param online_id: The member's online id.
        :param account_id: The member's account id.
        """
        await self._require(CHAT_WRITES)
        target_account_id = await _identity.account_id_for(self._session, online_id, account_id)
        await self._session.delete(f"{_GAMING_LOUNGE_URI}/groups/{group_id}/members/{target_account_id}")

    async def leave_group(self, group_id: str) -> bool:
        """Leave a chat group. Destructive, so the membership is read first.

        :param group_id: The group's id.
        :returns: ``True`` when PSN was told to remove the caller; ``False`` when the caller is not a member
            of that group, in which case nothing is sent.
        """
        await self._require(CHAT_WRITES)
        if group_id not in await self._social_client.chat_group_ids():
            return False
        await self._session.delete(f"{_GAMING_LOUNGE_URI}/groups/{group_id}/members/me")
        return True

    async def accept_friend(self, online_id: str | None = None, account_id: str | None = None) -> None:
        """Accept a friend request from (or send one to) a user.

        PSN uses the same call to send a request and to accept one.

        :param online_id: The other user's online id.
        :param account_id: The other user's account id.
        """
        await self._require(FRIEND_WRITES)
        target_account_id = await _identity.account_id_for(self._session, online_id, account_id)
        await self._session.put(f"{_PROFILE_URI}/me/friends/{target_account_id}")

    async def send_friend_request(self, online_id: str) -> None:
        """Send a friend request to ``online_id``.

        :param online_id: The other user's online id.
        """
        await self.accept_friend(online_id=online_id)

    async def accept_friend_request(self, online_id: str) -> None:
        """Accept the friend request ``online_id`` has sent the caller.

        :param online_id: The requester's online id, matched case-insensitively against the received
            requests.
        :raises NoPendingFriendRequestError: If that user has sent no request, so nothing is sent to PSN.
        """
        await self._require(FRIEND_WRITES)
        pending = await self._social_client.friend_requests()
        requester = next(
            (user for user in pending if user.online_id is not None and user.online_id.lower() == online_id.lower()),
            None,
        )
        if requester is None:
            raise NoPendingFriendRequestError(f"{online_id} has not sent a friend request.")
        await self._session.put(f"{_PROFILE_URI}/me/friends/{requester.account_id}")

    async def remove_friend(self, online_id: str | None = None, account_id: str | None = None) -> bool:
        """Remove a friend, or decline a pending friend request. Destructive, so the standing is read first.

        :param online_id: The other user's online id.
        :param account_id: The other user's account id.
        :returns: ``True`` when PSN was told to remove the relationship; ``False`` when there was nothing
            between the two accounts, in which case nothing is sent.
        """
        await self._require(FRIEND_WRITES)
        target_account_id = await _identity.account_id_for(self._session, online_id, account_id)
        standing = await self._social_client.friendship(account_id=target_account_id)
        if standing.relation == NO_FRIEND_RELATION:
            return False
        await self._session.delete(f"{_PROFILE_URI}/me/friends/{target_account_id}")
        return True


MutationServiceFactory = Callable[[str], Coroutine[Any, Any, "MutationService"]]
"""Builds a :class:`MutationService` (never cached) for a given Identity ``sub``. Requires an existing PSN
link. Backs ``curator.social_routes``."""
