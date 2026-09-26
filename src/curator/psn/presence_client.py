"""Async client for PSN presence data.

No persistence anywhere in this module -- presence is inherently live/ephemeral data; caching it would
just serve stale/wrong answers to the one question it exists to answer ("what is this user doing right
now").
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, Final

from curator.psn import _identity
from curator.psn._identity import ACCOUNT_ID_KEY
from curator.psn.models import Presence
from curator.psn.session import PsnSession

PROFILE_URI_V2: Final = "https://m.np.playstation.com/api/userProfile/v2/internal/users"

BASIC_PRESENCE_KEY: Final = "basicPresence"
BASIC_PRESENCES_KEY: Final = "basicPresences"
PRIMARY_PLATFORM_INFO_KEY: Final = "primaryPlatformInfo"
GAME_TITLE_INFO_LIST_KEY: Final = "gameTitleInfoList"
AVAILABILITY_KEY: Final = "availability"
ONLINE_STATUS_KEY: Final = "onlineStatus"
PLATFORM_KEY: Final = "platform"
LAST_ONLINE_DATE_KEY: Final = "lastOnlineDate"
TITLE_NAME_KEY: Final = "titleName"
ACCOUNT_IDS_PARAM: Final = "accountIds"


def basic_presences_url(account_id: str) -> str:
    return f"{PROFILE_URI_V2}/{account_id}/{BASIC_PRESENCES_KEY}"


def batch_basic_presences_url() -> str:
    return f"{PROFILE_URI_V2}/{BASIC_PRESENCES_KEY}"


def _presence_from_basic(basic: dict[str, Any]) -> Presence:
    """Build a :class:`~curator.psn.models.Presence` from a PSN ``basicPresence`` object."""
    platform_info = basic.get(PRIMARY_PLATFORM_INFO_KEY) or {}
    games = basic.get(GAME_TITLE_INFO_LIST_KEY) or []
    return Presence(
        online_status=basic.get(AVAILABILITY_KEY) or platform_info.get(ONLINE_STATUS_KEY),
        platform=platform_info.get(PLATFORM_KEY),
        last_online_date=platform_info.get(LAST_ONLINE_DATE_KEY),
        game_title=games[0].get(TITLE_NAME_KEY) if games else None,
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
                basic_presences_url(target_account_id),
                params={"type": "primary", "platforms": "PS4,PS5,MOBILE_APP,PSPC", "withOwnGameTitleInfo": "true"},
            )
        ).json()
        basic = data.get(BASIC_PRESENCE_KEY, data) if isinstance(data, dict) else {}
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
                batch_basic_presences_url(),
                params={
                    "type": "primary",
                    ACCOUNT_IDS_PARAM: ",".join(account_ids),
                    "platforms": "PS4,PS5,MOBILE_APP,PSPC",
                    "withOwnGameTitleInfo": "true",
                },
            )
        ).json()
        basic_presences = data.get(BASIC_PRESENCES_KEY) or [] if isinstance(data, dict) else []
        return {
            entry.get(ACCOUNT_ID_KEY): _presence_from_basic(entry)
            for entry in basic_presences
            if entry.get(ACCOUNT_ID_KEY) is not None
        }


PresenceClientFactory = Callable[[str], Coroutine[Any, Any, "PresenceClient"]]
"""Builds a raw :class:`PresenceClient` (never cached -- presence is live-only) for a given Identity
``sub``. Requires an existing PSN link. Lives alongside :class:`PresenceClient` (rather than in
``curator.app``, where it's built) so both ``curator.app`` and ``curator.presence_routes`` can import it
without the two importing each other -- mirrors ``curator.psn.trophy_client.TrophyClientFactory``."""
