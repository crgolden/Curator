"""Async client for PSN trophy data: summaries, per-title status, and raw trophy definitions merged with
earned progress.

Ported from ``psnpy.client.PsnAgent``'s trophy methods. ``rarest_trophies_for_title``'s derive/sort logic
was extracted out to :mod:`curator.psn.trophy_service` (pure, no I/O); results here are the source of truth
this client's caller may choose to wrap in :class:`~curator.psn.trophy_cache.CachedTrophyClient`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from curator.psn import _identity
from curator.psn.models import (
    TitleStat,
    TrophyDetail,
    TrophyGroup,
    TrophyGroups,
    TrophySummary,
    TrophyTitle,
    trophy_counts,
)
from curator.psn.session import PsnSession

if TYPE_CHECKING:
    from curator.psn.trophy_cache import CachedTrophyClient

_TROPHIES_URI = "https://m.np.playstation.com/api/trophy/v1"
_GAMES_LIST_URI = "https://m.np.playstation.com/api/gamelist/v2"

# Maps PSN's raw title-stats "category" value to the platform name this client exposes.
_PLATFORM_CATEGORY_NAMES = {"ps4_game": "PS4", "ps5_native_game": "PS5"}

_PLAY_DURATION_RE = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")


def _trophy_service_name(platform: str) -> str:
    """Return PSN's trophy service name for a platform string: ``"trophy2"`` for PS5, else ``"trophy"``."""
    return "trophy2" if platform.upper() == "PS5" else "trophy"


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


def _trophy_title(data: dict[str, Any]) -> TrophyTitle:
    """Map a raw ``trophyTitles``-endpoint entry to our :class:`~curator.psn.models.TrophyTitle`."""
    return TrophyTitle(
        name=data.get("trophyTitleName"),
        np_communication_id=data.get("npCommunicationId"),
        platforms=tuple(p for p in (data.get("trophyTitlePlatform") or "").split(",") if p),
        progress=data.get("progress"),
        earned=trophy_counts(data.get("earnedTrophies")),
        defined=trophy_counts(data.get("definedTrophies")),
        last_updated=data.get("lastUpdatedDateTime"),
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
            path_id = "me"
            resolved_account_id = await _identity.own_account_id(self._session)
        else:
            resolved_account_id = await _identity.account_id_for(self._session, online_id, account_id)
            path_id = resolved_account_id
        data = (await self._session.get(f"{_TROPHIES_URI}/users/{path_id}/trophySummary")).json()
        return TrophySummary(
            level=data.get("trophyLevel", -1),
            progress=data.get("progress", -1),
            tier=data.get("tier", -1),
            earned=trophy_counts(data.get("earnedTrophies")),
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
                await self._session.get(
                    f"{_TROPHIES_URI}/users/{path_id}/trophyTitles",
                    params={"limit": page_limit, "offset": offset},
                )
            ).json()
            entries = response.get("trophyTitles") or []
            if not entries:
                break
            titles.extend(_trophy_title(entry) for entry in entries)
            offset += len(entries)
            if (response.get("nextOffset") or 0) <= 0:
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
            await self._session.get(
                f"{_TROPHIES_URI}/users/{path_id}/titles/trophyTitles",
                params={"npTitleIds": ",".join(title_ids)},
            )
        ).json()
        titles: list[TrophyTitle] = []
        for title in response.get("titles") or []:
            for entry in title.get("trophyTitles") or []:
                titles.append(_trophy_title(entry))
        return titles

    async def title_trophies(
        self,
        np_communication_id: str,
        platform: str,
        online_id: str | None = None,
        account_id: str | None = None,
        group: str = "all",
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
        meta_url = f"{_TROPHIES_URI}/npCommunicationIds/{np_communication_id}/trophyGroups/{group}/trophies"
        progress_url = (
            f"{_TROPHIES_URI}/users/{path_id}/npCommunicationIds/{np_communication_id}/trophyGroups/{group}/trophies"
        )
        details: list[TrophyDetail] = []
        offset = 0
        page_size = 200
        while limit is None or len(details) < limit:
            page_limit = page_size if limit is None else min(page_size, limit - len(details))
            params = {"npServiceName": service_name, "limit": page_limit, "offset": offset}
            meta_response = (await self._session.get(meta_url, params=params)).json()
            progress_response = (await self._session.get(progress_url, params=params)).json()
            trophies = meta_response.get("trophies") or []
            progresses = progress_response.get("trophies") or []
            if not trophies:
                break
            for trophy, progress in zip(trophies, progresses, strict=False):
                merged = {**trophy, **progress}
                details.append(
                    TrophyDetail(
                        trophy_id=merged.get("trophyId"),
                        name=merged.get("trophyName"),
                        detail=merged.get("trophyDetail"),
                        type=merged.get("trophyType"),
                        hidden=merged.get("trophyHidden"),
                        icon_url=merged.get("trophyIconUrl"),
                        earned=merged.get("earned"),
                        earned_date=merged.get("earnedDateTime"),
                        progress_rate=merged.get("progressRate"),
                        rarity=_to_float(merged.get("trophyEarnedRate")),
                    )
                )
            offset += len(trophies)
            if (meta_response.get("nextOffset") or 0) <= 0:
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
        meta = (
            await self._session.get(
                f"{_TROPHIES_URI}/npCommunicationIds/{np_communication_id}/trophyGroups",
                params=params,
            )
        ).json()
        progress = (
            await self._session.get(
                f"{_TROPHIES_URI}/users/{path_id}/npCommunicationIds/{np_communication_id}/trophyGroups",
                params=params,
            )
        ).json()
        merged = {**meta, **progress}
        merged_groups = [
            {**meta_group, **progress_group}
            for meta_group, progress_group in zip(
                meta.get("trophyGroups") or [],
                progress.get("trophyGroups") or [],
                strict=False,
            )
        ]
        groups = tuple(
            TrophyGroup(
                group_id=group.get("trophyGroupId"),
                name=group.get("trophyGroupName"),
                detail=group.get("trophyGroupDetail"),
                icon_url=group.get("trophyGroupIconUrl"),
                progress=group.get("progress"),
                defined=trophy_counts(group.get("definedTrophies")),
                earned=trophy_counts(group.get("earnedTrophies")),
                last_updated=group.get("lastUpdatedDateTime"),
            )
            for group in merged_groups
        )
        return TrophyGroups(
            title_name=merged.get("trophyTitleName"),
            platforms=tuple(sorted(p for p in (merged.get("trophyTitlePlatform") or "").split(",") if p)),
            progress=merged.get("progress"),
            defined=trophy_counts(merged.get("definedTrophies")),
            earned=trophy_counts(merged.get("earnedTrophies")),
            groups=groups,
            last_updated=merged.get("lastUpdatedDateTime"),
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
                await self._session.get(
                    f"{_GAMES_LIST_URI}/users/{path_id}/titles",
                    params={"limit": page_limit, "offset": offset},
                )
            ).json()
            titles = response.get("titles") or []
            if not titles:
                break
            for title in titles:
                stats.append(
                    TitleStat(
                        title_id=title.get("titleId"),
                        name=title.get("name"),
                        category=_PLATFORM_CATEGORY_NAMES.get(title.get("category"), "UNKNOWN"),
                        play_count=title.get("playCount"),
                        first_played=title.get("firstPlayedDateTime"),
                        last_played=title.get("lastPlayedDateTime"),
                        play_duration_seconds=_play_duration_seconds(title.get("playDuration")),
                        image_url=title.get("imageUrl"),
                    )
                )
            offset += len(titles)
            if (response.get("nextOffset") or 0) <= 0:
                break
        return stats


TrophyClientFactory = Callable[[str], Coroutine[Any, Any, "TrophyClient | CachedTrophyClient"]]
"""Builds a trophy client (:class:`~curator.psn.trophy_cache.CachedTrophyClient` when Redis is configured,
else a raw :class:`TrophyClient`) for a given Identity ``sub``. Requires an existing PSN link -- unlike
``curator.link_service.AgentFactory``, there is no ``npsso`` bootstrap path here. Lives alongside
:class:`TrophyClient` (rather than in ``curator.app``, where it's built) so both ``curator.app`` and
``curator.trophy_routes`` can import it without the two importing each other."""
