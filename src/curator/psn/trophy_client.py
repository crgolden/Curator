"""Async client for PSN trophy data: summaries, per-title status, and raw trophy definitions merged with
earned progress.

Results here are the source of truth this client's caller may choose to wrap in
:class:`~curator.psn.trophy_cache.CachedTrophyClient`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any, Final

from curator.psn import _identity
from curator.psn.models import (
    ALL_TROPHY_GROUPS,
    TitleStat,
    TrophyDetail,
    TrophyGroup,
    TrophyGroups,
    TrophySummary,
    TrophyTitle,
    trophy_counts,
)
from curator.psn.session import PsnSession
from curator.psn.title_platform import PS4, PS5

if TYPE_CHECKING:
    from curator.psn.trophy_cache import CachedTrophyClient

TROPHIES_URI: Final = "https://m.np.playstation.com/api/trophy/v1"
GAMES_LIST_URI: Final = "https://m.np.playstation.com/api/gamelist/v2"

PS4_GAME_CATEGORY: Final = "ps4_game"
PS5_NATIVE_GAME_CATEGORY: Final = "ps5_native_game"
UNKNOWN_CATEGORY: Final = "UNKNOWN"

_PLATFORM_CATEGORY_NAMES = {PS4_GAME_CATEGORY: PS4, PS5_NATIVE_GAME_CATEGORY: PS5}

TROPHY_LEVEL_KEY: Final = "trophyLevel"
PROGRESS_KEY: Final = "progress"
TIER_KEY: Final = "tier"
EARNED_TROPHIES_KEY: Final = "earnedTrophies"
DEFINED_TROPHIES_KEY: Final = "definedTrophies"
TROPHY_TITLES_KEY: Final = "trophyTitles"
TROPHY_TITLE_NAME_KEY: Final = "trophyTitleName"
NP_COMMUNICATION_ID_KEY: Final = "npCommunicationId"
TROPHY_TITLE_PLATFORM_KEY: Final = "trophyTitlePlatform"
LAST_UPDATED_KEY: Final = "lastUpdatedDateTime"
NEXT_OFFSET_KEY: Final = "nextOffset"
TITLES_KEY: Final = "titles"
TROPHIES_KEY: Final = "trophies"
TROPHY_ID_KEY: Final = "trophyId"
TROPHY_NAME_KEY: Final = "trophyName"
TROPHY_DETAIL_KEY: Final = "trophyDetail"
TROPHY_TYPE_KEY: Final = "trophyType"
TROPHY_HIDDEN_KEY: Final = "trophyHidden"
TROPHY_ICON_URL_KEY: Final = "trophyIconUrl"
EARNED_KEY: Final = "earned"
EARNED_DATE_KEY: Final = "earnedDateTime"
PROGRESS_RATE_KEY: Final = "progressRate"
TROPHY_EARNED_RATE_KEY: Final = "trophyEarnedRate"
TROPHY_GROUPS_KEY: Final = "trophyGroups"
TROPHY_GROUP_ID_KEY: Final = "trophyGroupId"
TROPHY_GROUP_NAME_KEY: Final = "trophyGroupName"
TROPHY_GROUP_DETAIL_KEY: Final = "trophyGroupDetail"
TROPHY_GROUP_ICON_URL_KEY: Final = "trophyGroupIconUrl"
TITLE_ID_KEY: Final = "titleId"
NAME_KEY: Final = "name"
CATEGORY_KEY: Final = "category"
PLAY_COUNT_KEY: Final = "playCount"
FIRST_PLAYED_KEY: Final = "firstPlayedDateTime"
LAST_PLAYED_KEY: Final = "lastPlayedDateTime"
PLAY_DURATION_KEY: Final = "playDuration"
IMAGE_URL_KEY: Final = "imageUrl"

_PLAY_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def trophy_summary_url(path_id: str) -> str:
    return f"{TROPHIES_URI}/users/{path_id}/trophySummary"


def trophy_titles_url(path_id: str) -> str:
    return f"{TROPHIES_URI}/users/{path_id}/{TROPHY_TITLES_KEY}"


def title_trophy_titles_url(path_id: str) -> str:
    return f"{TROPHIES_URI}/users/{path_id}/{TITLES_KEY}/{TROPHY_TITLES_KEY}"


def trophy_groups_url(np_communication_id: str) -> str:
    return f"{TROPHIES_URI}/npCommunicationIds/{np_communication_id}/{TROPHY_GROUPS_KEY}"


def user_trophy_groups_url(path_id: str, np_communication_id: str) -> str:
    return f"{TROPHIES_URI}/users/{path_id}/npCommunicationIds/{np_communication_id}/{TROPHY_GROUPS_KEY}"


def group_trophies_url(np_communication_id: str, group: str) -> str:
    return f"{trophy_groups_url(np_communication_id)}/{group}/{TROPHIES_KEY}"


def user_group_trophies_url(path_id: str, np_communication_id: str, group: str) -> str:
    return f"{user_trophy_groups_url(path_id, np_communication_id)}/{group}/{TROPHIES_KEY}"


def title_stats_url(path_id: str) -> str:
    return f"{GAMES_LIST_URI}/users/{path_id}/{TITLES_KEY}"


def _trophy_service_name(platform: str) -> str:
    """Return PSN's trophy service name for a platform string: ``"trophy2"`` for PS5, else ``"trophy"``."""
    return "trophy2" if platform.upper() == PS5 else "trophy"


def _to_float(value: Any) -> float | None:
    """Coerce a PSN numeric field to float. PSN returns some numbers (e.g. trophy earn rate) as strings."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _play_duration_seconds(play_duration: str | None) -> int | None:
    """Parse PSN's ISO-8601-like play-duration string (e.g. ``"PT243H18M48S"``) to whole seconds."""
    if not play_duration:
        return None
    match = _PLAY_DURATION_RE.search(play_duration)
    if not match:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _platforms(value: Any) -> tuple[str, ...]:
    return tuple(p for p in (value or "").split(",") if p)


def _trophy_title(data: dict[str, Any]) -> TrophyTitle:
    """Map a raw ``trophyTitles``-endpoint entry to our :class:`~curator.psn.models.TrophyTitle`."""
    return TrophyTitle(
        name=data.get(TROPHY_TITLE_NAME_KEY),
        np_communication_id=data.get(NP_COMMUNICATION_ID_KEY),
        platforms=_platforms(data.get(TROPHY_TITLE_PLATFORM_KEY)),
        progress=data.get(PROGRESS_KEY),
        earned=trophy_counts(data.get(EARNED_TROPHIES_KEY)),
        defined=trophy_counts(data.get(DEFINED_TROPHIES_KEY)),
        last_updated=data.get(LAST_UPDATED_KEY),
    )


class TrophyClient:
    """PSN trophy operations, targetable at any user (default: the authenticated user).

    :param session: The authenticated :class:`~curator.psn.session.PsnSession` to call through.
    """

    def __init__(self, session: PsnSession) -> None:
        self._session = session

    async def trophy_summary(self, online_id: str | None = None, account_id: str | None = None) -> TrophySummary:
        """Get a user's overall trophy standing (level, tier, earned counts).

        :param online_id: Target user's online id; omit (with ``account_id``) for the authenticated user.
        :param account_id: Target user's account id.
        :returns: The :class:`~curator.psn.models.TrophySummary`.
        """
        return await self._session.run_with_reauth(lambda: self._trophy_summary(online_id, account_id))

    async def _trophy_summary(self, online_id: str | None, account_id: str | None) -> TrophySummary:
        if online_id is None and account_id is None:
            path_id = _identity.SELF_PATH_ID
            resolved_account_id = await _identity.own_account_id(self._session)
        else:
            resolved_account_id = await _identity.account_id_for(self._session, online_id, account_id)
            path_id = resolved_account_id
        data = (await self._session.get(trophy_summary_url(path_id))).json()
        return TrophySummary(
            level=data.get(TROPHY_LEVEL_KEY, -1),
            progress=data.get(PROGRESS_KEY, -1),
            tier=data.get(TIER_KEY, -1),
            earned=trophy_counts(data.get(EARNED_TROPHIES_KEY)),
            account_id=resolved_account_id,
        )

    async def trophy_titles(
        self,
        online_id: str | None = None,
        account_id: str | None = None,
        limit: int = 100,
    ) -> list[TrophyTitle]:
        """List a user's games that have trophies, with per-game progress.

        :param online_id: Target user's online id; omit for the authenticated user.
        :param account_id: Target user's account id.
        :param limit: Maximum number of titles to return.
        :returns: A list of :class:`~curator.psn.models.TrophyTitle`.
        """
        return await self._session.run_with_reauth(lambda: self._trophy_titles(online_id, account_id, limit))

    async def _trophy_titles(self, online_id: str | None, account_id: str | None, limit: int) -> list[TrophyTitle]:
        path_id = await _identity.path_account_id(self._session, online_id, account_id)
        titles: list[TrophyTitle] = []
        offset = 0
        page_size = 50
        while len(titles) < limit:
            page_limit = min(page_size, limit - len(titles))
            response = (
                await self._session.get(trophy_titles_url(path_id), params={"limit": page_limit, "offset": offset})
            ).json()
            entries = response.get(TROPHY_TITLES_KEY) or []
            if not entries:
                break
            titles.extend(_trophy_title(entry) for entry in entries)
            offset += len(entries)
            if (response.get(NEXT_OFFSET_KEY) or 0) <= 0:
                break
        return titles

    async def trophy_titles_for_title(
        self,
        title_ids: list[str],
        online_id: str | None = None,
        account_id: str | None = None,
    ) -> list[TrophyTitle]:
        """Get a user's trophy summary for specific title ids (e.g. to check progress on a known game).

        :param title_ids: The titles' npTitleIds (e.g. ``["CUSA00419_00"]``).
        :param online_id: Target user's online id; omit for the authenticated user.
        :param account_id: Target user's account id.
        :returns: A list of :class:`~curator.psn.models.TrophyTitle` (one per title that has trophy data).
        """
        return await self._session.run_with_reauth(
            lambda: self._trophy_titles_for_title(title_ids, online_id, account_id)
        )

    async def _trophy_titles_for_title(
        self,
        title_ids: list[str],
        online_id: str | None,
        account_id: str | None,
    ) -> list[TrophyTitle]:
        path_id = await _identity.path_account_id(self._session, online_id, account_id)
        response = (
            await self._session.get(title_trophy_titles_url(path_id), params={"npTitleIds": ",".join(title_ids)})
        ).json()
        titles: list[TrophyTitle] = []
        for title in response.get(TITLES_KEY) or []:
            for entry in title.get(TROPHY_TITLES_KEY) or []:
                titles.append(_trophy_title(entry))
        return titles

    async def title_trophies(
        self,
        np_communication_id: str,
        platform: str,
        online_id: str | None = None,
        account_id: str | None = None,
        group: str = ALL_TROPHY_GROUPS,
        limit: int | None = None,
    ) -> list[TrophyDetail]:
        """List every trophy in a title, each merged with the user's earned progress and rarity.

        :param np_communication_id: The title's ``npCommunicationId`` (from :meth:`trophy_titles`).
        :param platform: The title's platform, e.g. ``"PS5"`` or ``"PS4"`` (from :meth:`trophy_titles`).
        :param online_id: Target user's online id; omit for the authenticated user.
        :param account_id: Target user's account id.
        :param group: Trophy group id -- ``"all"``, ``"default"``, or ``"001"`` etc.
        :param limit: Maximum number of trophies to return; ``None`` returns all.
        :returns: A list of :class:`~curator.psn.models.TrophyDetail`.
        """
        return await self._session.run_with_reauth(
            lambda: self._title_trophies(np_communication_id, platform, online_id, account_id, group, limit)
        )

    async def _title_trophies(
        self,
        np_communication_id: str,
        platform: str,
        online_id: str | None,
        account_id: str | None,
        group: str,
        limit: int | None,
    ) -> list[TrophyDetail]:
        path_id = await _identity.path_account_id(self._session, online_id, account_id)
        service_name = _trophy_service_name(platform)
        meta_url = group_trophies_url(np_communication_id, group)
        progress_url = user_group_trophies_url(path_id, np_communication_id, group)
        details: list[TrophyDetail] = []
        offset = 0
        page_size = 200
        while limit is None or len(details) < limit:
            page_limit = page_size if limit is None else min(page_size, limit - len(details))
            params = {"npServiceName": service_name, "limit": page_limit, "offset": offset}
            meta_response = (await self._session.get(meta_url, params=params)).json()
            progress_response = (await self._session.get(progress_url, params=params)).json()
            trophies = meta_response.get(TROPHIES_KEY) or []
            progresses = progress_response.get(TROPHIES_KEY) or []
            if not trophies:
                break
            for trophy, progress in zip(trophies, progresses, strict=False):
                merged = {**trophy, **progress}
                details.append(
                    TrophyDetail(
                        trophy_id=merged.get(TROPHY_ID_KEY),
                        name=merged.get(TROPHY_NAME_KEY),
                        detail=merged.get(TROPHY_DETAIL_KEY),
                        type=merged.get(TROPHY_TYPE_KEY),
                        hidden=merged.get(TROPHY_HIDDEN_KEY),
                        icon_url=merged.get(TROPHY_ICON_URL_KEY),
                        earned=merged.get(EARNED_KEY),
                        earned_date=merged.get(EARNED_DATE_KEY),
                        progress_rate=merged.get(PROGRESS_RATE_KEY),
                        rarity=_to_float(merged.get(TROPHY_EARNED_RATE_KEY)),
                    )
                )
            offset += len(trophies)
            if (meta_response.get(NEXT_OFFSET_KEY) or 0) <= 0:
                break
        return details

    async def trophy_groups(
        self,
        np_communication_id: str,
        platform: str,
        online_id: str | None = None,
        account_id: str | None = None,
    ) -> TrophyGroups:
        """Get a title's trophy-group breakdown (base game + each DLC), with the user's earned progress.

        :param np_communication_id: The title's ``npCommunicationId`` (from :meth:`trophy_titles`).
        :param platform: The title's platform, e.g. ``"PS5"`` or ``"PS4"`` (from :meth:`trophy_titles`).
        :param online_id: Target user's online id; omit for the authenticated user.
        :param account_id: Target user's account id.
        :returns: The :class:`~curator.psn.models.TrophyGroups`.
        """
        return await self._session.run_with_reauth(
            lambda: self._trophy_groups(np_communication_id, platform, online_id, account_id)
        )

    async def _trophy_groups(
        self,
        np_communication_id: str,
        platform: str,
        online_id: str | None,
        account_id: str | None,
    ) -> TrophyGroups:
        path_id = await _identity.path_account_id(self._session, online_id, account_id)
        params = {"npServiceName": _trophy_service_name(platform)}
        meta = (await self._session.get(trophy_groups_url(np_communication_id), params=params)).json()
        progress = (await self._session.get(user_trophy_groups_url(path_id, np_communication_id), params=params)).json()
        merged = {**meta, **progress}
        merged_groups = [
            {**meta_group, **progress_group}
            for meta_group, progress_group in zip(
                meta.get(TROPHY_GROUPS_KEY) or [],
                progress.get(TROPHY_GROUPS_KEY) or [],
                strict=False,
            )
        ]
        groups = tuple(
            TrophyGroup(
                group_id=group.get(TROPHY_GROUP_ID_KEY),
                name=group.get(TROPHY_GROUP_NAME_KEY),
                detail=group.get(TROPHY_GROUP_DETAIL_KEY),
                icon_url=group.get(TROPHY_GROUP_ICON_URL_KEY),
                progress=group.get(PROGRESS_KEY),
                defined=trophy_counts(group.get(DEFINED_TROPHIES_KEY)),
                earned=trophy_counts(group.get(EARNED_TROPHIES_KEY)),
                last_updated=group.get(LAST_UPDATED_KEY),
            )
            for group in merged_groups
        )
        return TrophyGroups(
            title_name=merged.get(TROPHY_TITLE_NAME_KEY),
            platforms=tuple(sorted(_platforms(merged.get(TROPHY_TITLE_PLATFORM_KEY)))),
            progress=merged.get(PROGRESS_KEY),
            defined=trophy_counts(merged.get(DEFINED_TROPHIES_KEY)),
            earned=trophy_counts(merged.get(EARNED_TROPHIES_KEY)),
            groups=groups,
            last_updated=merged.get(LAST_UPDATED_KEY),
        )

    async def title_stats(
        self,
        online_id: str | None = None,
        account_id: str | None = None,
        limit: int = 200,
    ) -> list[TitleStat]:
        """List a user's played PS4/PS5 titles with playtime, play count, and first/last-played dates.

        .. note::
           PSN only returns play-stats for PS4-era titles and later; older platforms yield nothing.

        :param online_id: Target user's online id; omit for the authenticated user.
        :param account_id: Target user's account id.
        :param limit: Maximum number of titles to return.
        :returns: A list of :class:`~curator.psn.models.TitleStat`.
        """
        return await self._session.run_with_reauth(lambda: self._title_stats(online_id, account_id, limit))

    async def _title_stats(self, online_id: str | None, account_id: str | None, limit: int) -> list[TitleStat]:
        path_id = await _identity.path_account_id(self._session, online_id, account_id)
        stats: list[TitleStat] = []
        offset = 0
        page_size = 200
        while len(stats) < limit:
            page_limit = min(page_size, limit - len(stats))
            response = (
                await self._session.get(title_stats_url(path_id), params={"limit": page_limit, "offset": offset})
            ).json()
            titles = response.get(TITLES_KEY) or []
            if not titles:
                break
            for title in titles:
                stats.append(
                    TitleStat(
                        title_id=title.get(TITLE_ID_KEY),
                        name=title.get(NAME_KEY),
                        category=_PLATFORM_CATEGORY_NAMES.get(title.get(CATEGORY_KEY), UNKNOWN_CATEGORY),
                        play_count=title.get(PLAY_COUNT_KEY),
                        first_played=title.get(FIRST_PLAYED_KEY),
                        last_played=title.get(LAST_PLAYED_KEY),
                        play_duration_seconds=_play_duration_seconds(title.get(PLAY_DURATION_KEY)),
                        image_url=title.get(IMAGE_URL_KEY),
                    )
                )
            offset += len(titles)
            if (response.get(NEXT_OFFSET_KEY) or 0) <= 0:
                break
        return stats


TrophyClientFactory = Callable[[str], Coroutine[Any, Any, "TrophyClient | CachedTrophyClient"]]
"""Builds a trophy client (:class:`~curator.psn.trophy_cache.CachedTrophyClient` when Redis is configured,
else a raw :class:`TrophyClient`) for a given Identity ``sub``. Requires an existing PSN link -- unlike
``curator.link_service.AgentFactory``, there is no ``npsso`` bootstrap path here. Lives alongside
:class:`TrophyClient` (rather than in ``curator.app``, where it's built) so both ``curator.app`` and
``curator.trophy_routes`` can import it without the two importing each other."""
