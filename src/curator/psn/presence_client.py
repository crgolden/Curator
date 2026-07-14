"""Async client for PSN presence data.

No persistence anywhere in this module -- presence is inherently live/ephemeral data; caching it would
just serve stale/wrong answers to the one question it exists to answer ("what is this user doing right
now").
"""

from __future__ import annotations

from typing import Any

from curator.psn import _identity
from curator.psn.models import Presence
from curator.psn.session import PsnSession

_PROFILE_URI_V2 = "https://m.np.playstation.com/api/userProfile/v2/internal/users"


def _presence_from_basic(basic: dict[str, Any]) -> Presence:
    """Build a :class:`~curator.psn.models.Presence` from a PSN ``basicPresence`` object."""
    platform_info = basic.get("primaryPlatformInfo") or {}
    games = basic.get("gameTitleInfoList") or []
    return Presence(
        online_status=basic.get("availability") or platform_info.get("onlineStatus"),
        platform=platform_info.get("platform"),
        last_online_date=platform_info.get("lastOnlineDate"),
        game_title=games[0].get("titleName") if games else None,
    )


class PresenceClient:
    """PSN online-presence operations.

    :param session: The authenticated :class:`~curator.psn.session.PsnSession` to call through.
    """

    def __init__(self, session: PsnSession) -> None:
        self._session = session

    async def presence(self, online_id: str | None = None, account_id: str | None = None) -> Presence:
        """Get a user's current online presence (status, platform, current game).

        :param online_id: Target user's online id; omit for the authenticated user.
        :param account_id: Target user's account id.
        :returns: The :class:`~curator.psn.models.Presence`.
        """
        return await self._session.run_with_reauth(lambda: self._presence(online_id, account_id))

    async def _presence(self, online_id: str | None, account_id: str | None) -> Presence:
        target_account_id = await _identity.account_id_for(self._session, online_id, account_id)
        data = (
            await self._session.get(
                f"{_PROFILE_URI_V2}/{target_account_id}/basicPresences",
                params={"type": "primary", "platforms": "PS4,PS5,MOBILE_APP,PSPC", "withOwnGameTitleInfo": "true"},
            )
        ).json()
        basic = data.get("basicPresence", data) if isinstance(data, dict) else {}
        return _presence_from_basic(basic)

    async def presence_batch(self, account_ids: list[str]) -> dict[str, Presence]:
        """Get the current presence of many accounts in one request, keyed by account id.

        More efficient than calling :meth:`presence` per user.

        :param account_ids: The target account ids.
        :returns: A dict mapping each account id to its :class:`~curator.psn.models.Presence`.
        """
        return await self._session.run_with_reauth(lambda: self._presence_batch(account_ids))

    async def _presence_batch(self, account_ids: list[str]) -> dict[str, Presence]:
        data = (
            await self._session.get(
                f"{_PROFILE_URI_V2}/basicPresences",
                params={
                    "type": "primary",
                    "accountIds": ",".join(account_ids),
                    "platforms": "PS4,PS5,MOBILE_APP,PSPC",
                    "withOwnGameTitleInfo": "true",
                },
            )
        ).json()
        basic_presences = data.get("basicPresences") or [] if isinstance(data, dict) else []
        return {
            entry.get("accountId"): _presence_from_basic(entry)
            for entry in basic_presences
            if entry.get("accountId") is not None
        }
