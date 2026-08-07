"""Async client for PSN library data: owned entitlements, recently played, and purchased games.

``entitlements()`` (ported from ``psnpy.client.PsnAgent.entitlements``) is the curation pipeline's
ingestion source, called via ``curator.library.ingestion_service`` for the *caller's own* PSN entitlements.
``recently_played()``/``purchased()`` are ported for full-fidelity parity but aren't currently consumed by
curation.
"""

from __future__ import annotations

from typing import Any

from curator.psn._graphql import run_persisted_query
from curator.psn.models import Entitlement, LibraryGame
from curator.psn.session import PsnSession

_ENTITLEMENTS_URL = "https://m.np.playstation.com/api/entitlement/v2/users/me/internal/entitlements"

_OP_RECENTLY_PLAYED = ("getUserGameList", "e780a6d8b921ef0c59ec01ea5c5255671272ca0d819edb61320914cf7a78b3ae")
_OP_PURCHASED = ("getPurchasedGameList", "2c045408b0a4d0264bb5a3edfed4efd49fb4749cf8d216be9043768adff905e2")

_GRAPHQL_HEADERS = {"apollo-require-preflight": "true"}


def _library_game(game: dict[str, Any]) -> LibraryGame:
    """Map a PSN ``GameLibraryTitle`` (recently-played or purchased) to our :class:`LibraryGame`."""
    platform = game.get("platform")
    if isinstance(platform, (list, tuple)):
        platform = "/".join(str(p) for p in platform) or None
    return LibraryGame(
        title_id=game.get("titleId"),
        name=game.get("name"),
        platform=platform,
        concept_id=game.get("conceptId"),
        product_id=game.get("productId"),
        image_url=(game.get("image") or {}).get("url"),
        last_played=game.get("lastPlayedDateTime"),
        is_active=game.get("isActive"),
    )


class LibraryClient:
    """PSN library operations for the authenticated user: entitlements, recently played, purchased.

    :param session: The authenticated :class:`~curator.psn.session.PsnSession` to call through.
    """

    def __init__(self, session: PsnSession) -> None:
        self._session = session

    async def entitlements(self, limit: int | None = None) -> list[Entitlement]:
        """List the authenticated user's owned PS4/PS5 games and add-ons (entitlements).

        .. note::
           This is a self-only capability -- PSN exposes entitlements only for the authenticated account,
           and only for PS4/PS5 titles (the mobile-app endpoint is limited to those generations).

        :param limit: Maximum number of entitlements to return, or ``None`` (the default) to fetch every
            entitlement PSN reports -- matching ``psnawp_api``'s own ``game_entitlements(limit=None)``
            default. A libraries of many hundreds of titles is normal (PS+ Extra/Premium catalog titles,
            monthly games, and years of purchases all accumulate here), and this endpoint has no
            documented rate limit, so there is no reason to truncate by default.
        :returns: A list of :class:`~curator.psn.models.Entitlement`.
        """
        return [entitlement for entitlement, _raw in await self.entitlements_with_raw(limit=limit)]

    async def entitlements_with_raw(self, limit: int | None = None) -> list[tuple[Entitlement, dict[str, Any]]]:
        """As :meth:`entitlements`, but pairs each mapped entitlement with PSN's verbatim JSON entry.

        Ingestion persists the raw entry alongside the extracted columns (``entitlement_snapshots.raw``)
        so a mapping bug here can never permanently lose a field PSN sent -- the payload carries roughly
        three times what we extract. Callers that only want the mapped view should use
        :meth:`entitlements`.

        :param limit: As :meth:`entitlements`.
        :returns: ``(entitlement, raw_entry)`` pairs, in PSN's order.
        """
        return await self._session.run_with_reauth(lambda: self._entitlements(limit))

    async def _entitlements(self, limit: int | None) -> list[tuple[Entitlement, dict[str, Any]]]:
        entitlements: list[tuple[Entitlement, dict[str, Any]]] = []
        offset = 0
        page_size = 20
        while limit is None or len(entitlements) < limit:
            page_limit = page_size if limit is None else min(page_size, limit - len(entitlements))
            response = (
                await self._session.get(
                    _ENTITLEMENTS_URL,
                    params={
                        "entitlementType": "1,2,3,4,5",
                        "fields": "titleMeta,gameMeta,conceptMeta,rewardMeta,rewardMeta.retentionPolicy,"
                        "rewardMeta.rewardMembershipType",
                        "gameMetaPackageType": "PSGD,PS4GD",
                        "titleId": "",
                        "limit": page_limit,
                        "offset": offset,
                    },
                )
            ).json()
            entries = response.get("entitlements") or []
            if not entries:
                break
            for entry in entries:
                game_meta = entry.get("gameMeta") or {}
                title_meta = entry.get("titleMeta") or {}
                concept_meta = entry.get("conceptMeta") or {}
                attributes = entry.get("entitlementAttributes") or []
                platform_ids = tuple(
                    attribute["platformId"]
                    for attribute in attributes
                    if isinstance(attribute, dict) and attribute.get("platformId")
                )
                entitlements.append(
                    (
                        Entitlement(
                            entitlement_id=entry.get("id"),
                            name=game_meta.get("name") or title_meta.get("name"),
                            title_id=title_meta.get("titleId"),
                            concept_id=concept_meta.get("conceptId"),
                            product_id=entry.get("productId"),
                            sku_id=entry.get("skuId"),
                            package_type=game_meta.get("packageType"),
                            game_type=game_meta.get("type"),
                            active=entry.get("activeFlag"),
                            active_date=entry.get("activeDate"),
                            image_url=title_meta.get("imageUrl") or game_meta.get("iconUrl"),
                            title_image_url=title_meta.get("imageUrl"),
                            game_icon_url=game_meta.get("iconUrl"),
                            concept_icon_url=concept_meta.get("iconUrl"),
                            is_game=entry.get("isGame"),
                            platform_ids=platform_ids,
                            game_meta_name=game_meta.get("name"),
                            concept_meta_name=concept_meta.get("name"),
                            title_meta_name=title_meta.get("name"),
                        ),
                        entry,
                    )
                )
            offset += len(entries)
            if len(entries) < page_limit or offset >= response.get("totalResults", 0):
                break
        return entitlements

    async def recently_played(self, limit: int = 100) -> list[LibraryGame]:
        """List the authenticated user's recently played games (PS3/PS4/PS5), newest first.

        Uses PSN's GraphQL ``getUserGameList``. Complements ``psn.trophy_client``'s ``title_stats``, which
        returns richer per-title playtime for PS4/PS5 only.

        :param limit: Maximum number of titles to return.
        :returns: A list of :class:`~curator.psn.models.LibraryGame` (``last_played`` populated).
        """
        return await self._session.run_with_reauth(lambda: self._recently_played(limit))

    async def _recently_played(self, limit: int) -> list[LibraryGame]:
        response = await run_persisted_query(
            self._session,
            _OP_RECENTLY_PLAYED,
            {"categories": "ps3_game,ps4_game,ps5_native_game", "limit": limit},
            headers=_GRAPHQL_HEADERS,
        )
        data = response.get("data") or {}
        games = (data.get("gameLibraryTitlesRetrieve") or {}).get("games") or []
        return [_library_game(game) for game in games]

    async def purchased(self, limit: int = 500) -> list[LibraryGame]:
        """List the authenticated user's purchased games (PS3/PS4/PS5).

        Uses PSN's GraphQL ``getPurchasedGameList``. Overlaps :meth:`entitlements` (owned PS4/PS5
        games/add-ons) but spans PS3 and is purchase-scoped.

        :param limit: Maximum number of titles to return.
        :returns: A list of :class:`~curator.psn.models.LibraryGame`.
        """
        return await self._session.run_with_reauth(lambda: self._purchased(limit))

    async def _purchased(self, limit: int) -> list[LibraryGame]:
        response = await run_persisted_query(
            self._session,
            _OP_PURCHASED,
            {
                "isActive": True,
                "platform": ["ps3", "ps4", "ps5"],
                "start": 0,
                "size": limit,
                "subscriptionService": "NONE",
            },
            headers=_GRAPHQL_HEADERS,
        )
        data = response.get("data") or {}
        games = (data.get("purchasedTitlesRetrieve") or {}).get("games") or []
        return [_library_game(game) for game in games]
