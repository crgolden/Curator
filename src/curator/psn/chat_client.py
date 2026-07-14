"""Async client for PSN chat groups and messages -- reads only.

Mutating chat operations (create/rename group, send message, invite/kick, leave) live in
:mod:`curator.psn.mutation_service`, gated by the mutation-safety wall (:mod:`curator.psn.safety`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from curator.psn.models import ChatGroup, ChatMessage, SocialUser
from curator.psn.session import PsnSession

_GAMING_LOUNGE_URI = "https://m.np.playstation.com/api/gamingLoungeGroups/v1"


def epoch_millis_iso(value: Any) -> str | None:
    """Convert a PSN epoch-milliseconds timestamp (string or int) to an ISO-8601 UTC string."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _chat_group(info: dict[str, Any]) -> ChatGroup:
    """Map a PSN group-information payload to our :class:`~curator.psn.models.ChatGroup`."""
    members = info.get("members") or []
    name = ((info.get("groupName") or {}).get("value") or "").strip() or None
    return ChatGroup(
        group_id=info.get("groupId"),
        name=name,
        favorite=info.get("isFavorite"),
        member_count=len(members),
        members=tuple(SocialUser(account_id=m.get("accountId"), online_id=m.get("onlineId")) for m in members),
        modified_at=epoch_millis_iso(info.get("modifiedTimestamp")),
    )


class ChatClient:
    """PSN chat-group read operations.

    :param session: The authenticated :class:`~curator.psn.session.PsnSession` to call through.
    """

    def __init__(self, session: PsnSession) -> None:
        self._session = session

    async def chat_groups(self, limit: int = 200) -> list[ChatGroup]:
        """List the chat groups (gaming-lounge threads) the authenticated user participates in.

        .. note::
           PSN's group-list endpoint returns only group ids, so each group's name and members are fetched
           with one request per group (an N+1); a single-request path is a planned raw-endpoint optimization.

        :param limit: Maximum number of groups to return.
        :returns: A list of :class:`~curator.psn.models.ChatGroup`.
        """
        return await self._session.run_with_reauth(lambda: self._chat_groups(limit))

    async def _chat_groups(self, limit: int) -> list[ChatGroup]:
        response = (
            await self._session.get(
                f"{_GAMING_LOUNGE_URI}/members/me/groups",
                params={"includeFields": "members", "limit": limit},
            )
        ).json()
        group_ids = [g["groupId"] for g in response.get("groups") or []]
        return [await self._group_info(group_id) for group_id in group_ids]

    async def group_info(self, group_id: str) -> ChatGroup:
        """Get details (name, members, favorite flag) for a single chat group.

        :param group_id: The group's id (from :meth:`chat_groups`).
        :returns: The :class:`~curator.psn.models.ChatGroup`.
        """
        return await self._session.run_with_reauth(lambda: self._group_info(group_id))

    async def _group_info(self, group_id: str) -> ChatGroup:
        data = (
            await self._session.get(
                f"{_GAMING_LOUNGE_URI}/members/me/groups/{group_id}",
                params={
                    "includeFields": "groupName,groupIcon,members,mainThread,joinedTimestamp,modifiedTimestamp,"
                    "isFavorite,existsNewArrival,notificationSetting",
                },
            )
        ).json()
        return _chat_group(data)

    async def conversation(self, group_id: str, limit: int = 20) -> list[ChatMessage]:
        """Read a chat group's recent message history (newest messages first).

        :param group_id: The group's id (from :meth:`chat_groups`).
        :param limit: Maximum number of messages to return.
        :returns: A list of :class:`~curator.psn.models.ChatMessage`.
        """
        return await self._session.run_with_reauth(lambda: self._conversation(group_id, limit))

    async def _conversation(self, group_id: str, limit: int) -> list[ChatMessage]:
        data = (
            await self._session.get(
                f"{_GAMING_LOUNGE_URI}/members/me/groups/{group_id}/threads/{group_id}/messages",
                params={"limit": limit},
            )
        ).json()
        messages = data.get("messages") or [] if isinstance(data, dict) else []
        result: list[ChatMessage] = []
        for message in messages:
            sender = message.get("sender") or {}
            sender_account_id = sender.get("accountId")
            result.append(
                ChatMessage(
                    message_uid=message.get("messageUid"),
                    body=message.get("body"),
                    message_type=message.get("messageType"),
                    created_at=epoch_millis_iso(message.get("createdTimestamp")),
                    sender=SocialUser(account_id=str(sender_account_id), online_id=sender.get("onlineId"))
                    if sender_account_id is not None
                    else None,
                )
            )
        return result
