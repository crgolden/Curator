"""Async client for PSN social-graph and profile data, plus the mobile gateway's universal search.

No persistence anywhere in this module -- PSN is the source of truth for the social graph; every method
here is a live passthrough.

``metGetContextSearchResults`` is a **universal** search whose domain is the ``searchContext`` variable,
not a player search: :meth:`SocialClient.universal_search_players` and
:meth:`SocialClient.universal_search_games` are the same operation under two contexts and two persisted
hashes. It runs against the authenticated mobile gateway (``m.np.playstation.com``), which is a different
service from the anonymous storefront (``web.np.playstation.com``) that :mod:`curator.psn.store_client`
calls; their persisted-query hashes are not interchangeable, and the storefront has no search at all.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any, Final, Literal

from curator.psn import _identity
from curator.psn._graphql import run_persisted_query
from curator.psn._media import cover_image_url
from curator.psn.models import (
    AccountDevice,
    Friendship,
    GameSearchResult,
    PlayerSearchResult,
    Profile,
    ProfileShareLink,
    SocialUser,
)
from curator.psn.session import PsnSession

_PROFILE_URI = "https://m.np.playstation.com/api/userProfile/v1/internal/users"
_LEGACY_PROFILE_URI = "https://us-prof.np.community.playstation.net/userProfile/v1/users"
_MY_ACCOUNT_URL = "https://dms.api.playstation.com/api/v1/devices/accounts/me"
_CPSS_URI = "https://m.np.playstation.com/api/cpss"

_SEARCH_COMMON_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "apollographql-client-name": "PlayStationApp-Android",
    "apollographql-client-version": "25.4.0",
}
_OP_CONTEXT_SEARCH_SOCIAL = (
    "metGetContextSearchResults",
    "ac5fb2b82c4d086ca0d272fba34418ab327a7762dd2cd620e63f175bbc5aff10",
)
_OP_DOMAIN_SEARCH_SOCIAL = (
    "metGetDomainSearchResults",
    "23ece284bf8bdc50bfa30a4d97fd4d733e723beb7a42dff8c1ee883f8461a2e1",
)
_OP_CONTEXT_SEARCH_GAME = (
    "metGetContextSearchResults",
    "a2fbc15433b37ca7bfcd7112f741735e13268f5e9ebd5ffce51b85acc126f41d",
)
_OP_DOMAIN_SEARCH_GAME = (
    "metGetDomainSearchResults",
    "b51624299bd17b3799f77c9f097cc8887a04d3873f0329095976a841595bc902",
)

SOCIAL_SEARCH_CONTEXT: Final = "MobileUniversalSearchSocial"
GAME_SEARCH_CONTEXT: Final = "MobileUniversalSearchGame"

GameSearchDomain = Literal["MobileGames", "MobileAddOns"]

FULL_GAMES_DOMAIN: Final[GameSearchDomain] = "MobileGames"
ADD_ONS_DOMAIN: Final[GameSearchDomain] = "MobileAddOns"

MAX_GAME_SEARCH_PAGES: Final = 3
"""How many pages :meth:`SocialClient.universal_search_games` may fetch beyond the first.

Bounds spend, not recursion -- the loop already terminates on its own. Every page is a request to the
authenticated mobile gateway on one user's token, and an uncapped paginated fetch is how the admin
OpenCritic sweep once burned a whole day's quota on its first run; ``AdminRefreshMaxPages`` is that
incident's named constant and this is the same shape.

Three is what it takes to satisfy the largest ``limit`` ``GET /library/manual/search`` accepts (50)
against the smallest first page PSN has been observed to return (15, measured on ``"GTA"``); it has
returned 48 on other terms, where one extra page is already more than enough.
"""


def _profile(data: dict[str, Any]) -> Profile:
    """Map a legacy community-profile response to our :class:`~curator.psn.models.Profile`.

    ``personal_detail`` is deliberately left ``None`` regardless of what the response contains -- see the
    privacy-by-design note above :class:`~curator.psn.models.AccountDetails` in ``models.py``.
    """
    raw_profile = data.get("profile")
    profile: dict[str, Any] = raw_profile if isinstance(raw_profile, dict) else {}
    avatars = profile.get("avatarUrls") or []
    return Profile(
        about_me=profile.get("aboutMe"),
        avatars=tuple(
            str(avatar.get("avatarUrl")) for avatar in avatars if isinstance(avatar, dict) and avatar.get("avatarUrl")
        ),
        languages=tuple(profile.get("languagesUsed") or ()),
        is_officially_verified=bool(profile.get("isOfficiallyVerified", False)),
        personal_detail=None,
    )


def _player_search_result(item: dict[str, Any]) -> PlayerSearchResult:
    """Map a raw PSN universal-search user item to our :class:`~curator.psn.models.PlayerSearchResult`."""
    player = item.get("result") or {}
    return PlayerSearchResult(
        account_id=player.get("accountId"),
        online_id=player.get("onlineId"),
        avatar_url=player.get("avatarUrl"),
        is_ps_plus=player.get("isPsPlus"),
        relationship=player.get("relationshipState"),
    )


def _game_search_result(item: dict[str, Any]) -> GameSearchResult:
    """Map one raw ``searchResults`` entry to our :class:`~curator.psn.models.GameSearchResult`.

    Reads the nested ``result`` node rather than the enclosing item, which carries a composite ``id`` of
    its own that joins to nothing -- see :class:`~curator.psn.models.GameSearchResult`. A ``Concept`` and
    a ``Product`` node carry the same field names for everything read here except ``defaultProduct``,
    which only a concept has, so one mapper covers both; ``invariantName`` is the fallback title, being
    the untranslated form PSN sends when the locale has no ``name``.
    """
    result = item.get("result") or {}
    price = result.get("price") or {}
    default_product = result.get("defaultProduct") or {}
    return GameSearchResult(
        id=result.get("id"),
        kind=result.get("__typename"),
        default_product_id=default_product.get("id"),
        name=result.get("name") or result.get("invariantName"),
        platforms=tuple(str(platform) for platform in (result.get("platforms") or ())),
        cover_image_url=cover_image_url(result.get("media")),
        classification=result.get("localizedStoreDisplayClassification"),
        price=price.get("basePrice"),
        discounted_price=price.get("discountedPrice"),
        is_free=price.get("isFree"),
    )


def _domain_container(results_by_domain: list[Any], domain: GameSearchDomain) -> dict[str, Any]:
    """Pick the one result container whose own ``domain`` field names ``domain``.

    Selected by that field rather than by ordinal position. PSNAWP's ``SearchDomain`` IntEnum indexes this
    same list positionally (``FULL_GAMES = 0``, ``ADD_ONS = 1``), which matches what the live gateway
    returned but asserts an ordering PSN never promised and raises ``IndexError`` on a short list. Matching
    the label costs nothing and degrades to an empty result instead.
    """
    for entry in results_by_domain:
        if isinstance(entry, dict) and entry.get("domain") == domain:
            return entry
    return {}


class SocialClient:
    """PSN social-graph, profile, and device operations.

    :param session: The authenticated :class:`~curator.psn.session.PsnSession` to call through.
    """

    def __init__(self, session: PsnSession) -> None:
        self._session = session

    async def friends(
        self,
        online_id: str | None = None,
        account_id: str | None = None,
        limit: int = 1000,
    ) -> list[SocialUser]:
        """List a user's friends (account id + online id).

        .. note::
           Resolving each friend's online id currently costs one PSN request per friend.

        :param online_id: Target user's online id; omit for the authenticated user.
        :param account_id: Target user's account id.
        :param limit: Maximum number of friends to return (PSN caps this at 1000).
        :returns: A list of :class:`~curator.psn.models.SocialUser`.
        """
        return await self._session.run_with_reauth(lambda: self._friends(online_id, account_id, limit))

    async def _friends(self, online_id: str | None, account_id: str | None, limit: int) -> list[SocialUser]:
        path_id = (
            "me"
            if online_id is None and account_id is None
            else await _identity.account_id_for(self._session, online_id, account_id)
        )
        response = (
            await self._session.get(f"{_PROFILE_URI}/{path_id}/friends", params={"limit": min(1000, limit)})
        ).json()
        friend_ids = response.get("friends") or []
        return [
            SocialUser(account_id=fid, online_id=await _identity.online_id_for(self._session, fid))
            for fid in friend_ids
        ]

    async def blocked(self) -> list[SocialUser]:
        """List the authenticated user's blocked accounts.

        :returns: A list of :class:`~curator.psn.models.SocialUser`.
        """
        return await self._session.run_with_reauth(self._blocked)

    async def _blocked(self) -> list[SocialUser]:
        response = (await self._session.get(f"{_PROFILE_URI}/me/blocks")).json()
        block_ids = response.get("blockList") or []
        return [
            SocialUser(account_id=bid, online_id=await _identity.online_id_for(self._session, bid)) for bid in block_ids
        ]

    async def available_to_play(self) -> list[SocialUser]:
        """List the friends on the authenticated user's "Notify when available" subscription list.

        :returns: A list of :class:`~curator.psn.models.SocialUser`.
        """
        return await self._session.run_with_reauth(self._available_to_play)

    async def _available_to_play(self) -> list[SocialUser]:
        response = (await self._session.get(f"{_PROFILE_URI}/me/friends/subscribing/availableToPlay")).json()
        entries = response.get("settings") or []
        return [
            SocialUser(
                account_id=entry["accountId"],
                online_id=await _identity.online_id_for(self._session, entry["accountId"]),
            )
            for entry in entries
        ]

    async def friend_requests(self) -> list[SocialUser]:
        """List the friend requests the authenticated user has received.

        :returns: A list of :class:`~curator.psn.models.SocialUser`.
        """
        return await self._session.run_with_reauth(self._friend_requests)

    async def _friend_requests(self) -> list[SocialUser]:
        response = (await self._session.get(f"{_PROFILE_URI}/me/friends/receivedRequests")).json()
        requests = response.get("receivedRequests") or []
        return [
            SocialUser(
                account_id=entry["accountId"],
                online_id=await _identity.online_id_for(self._session, entry["accountId"]),
            )
            for entry in requests
        ]

    async def friendship(self, online_id: str | None = None, account_id: str | None = None) -> Friendship:
        """Get the authenticated user's friendship standing with another user.

        A target is required -- friendship is always relative to another account, never yourself.

        :param online_id: Target user's online id.
        :param account_id: Target user's account id.
        :returns: The :class:`~curator.psn.models.Friendship`.
        :raises ValueError: If neither ``online_id`` nor ``account_id`` is given.
        """
        if online_id is None and account_id is None:
            raise ValueError("friendship() requires a target user (online_id or account_id).")
        return await self._session.run_with_reauth(lambda: self._friendship(online_id, account_id))

    async def _friendship(self, online_id: str | None, account_id: str | None) -> Friendship:
        target_account_id = await _identity.account_id_for(self._session, online_id, account_id)
        data = (await self._session.get(f"{_PROFILE_URI}/me/friends/{target_account_id}/summary")).json()
        return Friendship(
            relation=data.get("friendRelation"),
            personal_detail_sharing=data.get("personalDetailSharing"),
            friends_count=data.get("friendsCount"),
            mutual_friends_count=data.get("mutualFriendsCount"),
        )

    async def profile(self, online_id: str | None = None, account_id: str | None = None) -> Profile:
        """Get a user's legacy public profile: about-me text, avatar URLs, languages, verification status.

        .. note::
           ``personal_detail`` is never populated -- see the privacy-by-design note above
           :class:`~curator.psn.models.AccountDetails` in ``models.py``.

        :param online_id: Target user's online id; omit for the authenticated user.
        :param account_id: Target user's account id.
        :returns: The :class:`~curator.psn.models.Profile`.
        """
        return await self._session.run_with_reauth(lambda: self._profile(online_id, account_id))

    async def _profile(self, online_id: str | None, account_id: str | None) -> Profile:
        resolved_online_id = await _identity.target_online_id(self._session, online_id, account_id)
        data = (
            await self._session.get(
                f"{_LEGACY_PROFILE_URI}/{resolved_online_id}/profile2",
                params={
                    "fields": "npId,onlineId,accountId,avatarUrls,plus,aboutMe,languagesUsed,"
                    "isOfficiallyVerified,personalDetail(@default,profilePictureUrls),"
                    "personalDetailSharing",
                },
            )
        ).json()
        return _profile(data)

    async def is_blocked(self, online_id: str | None = None, account_id: str | None = None) -> bool:
        """Check whether the authenticated user has blocked a specific user.

        :param online_id: Target user's online id.
        :param account_id: Target user's account id.
        :returns: ``True`` if the target is blocked.
        :raises ValueError: If neither ``online_id`` nor ``account_id`` is given.
        """
        if online_id is None and account_id is None:
            raise ValueError("is_blocked() requires a target user (online_id or account_id).")
        return await self._session.run_with_reauth(lambda: self._is_blocked(online_id, account_id))

    async def _is_blocked(self, online_id: str | None, account_id: str | None) -> bool:
        target_account_id = await _identity.account_id_for(self._session, online_id, account_id)
        response = (await self._session.get(f"{_PROFILE_URI}/me/blocks")).json()
        return target_account_id in (response.get("blockList") or [])

    async def universal_search_players(self, query: str, limit: int = 20) -> list[PlayerSearchResult]:
        """Run PSN's universal context search, scoped to the social domain.

        :param query: The search term.
        :param limit: Maximum number of results to return.
        :returns: A list of :class:`~curator.psn.models.PlayerSearchResult`.
        """
        return await self._session.run_with_reauth(lambda: self._universal_search_players(query, limit))

    async def _universal_search_players(self, query: str, limit: int) -> list[PlayerSearchResult]:
        response = await run_persisted_query(
            self._session,
            _OP_CONTEXT_SEARCH_SOCIAL,
            {"searchTerm": query, "searchContext": SOCIAL_SEARCH_CONTEXT, "displayTitleLocale": "en-US"},
            headers=_SEARCH_COMMON_HEADERS,
            check_errors=False,
        )
        results_by_domain = ((response.get("data") or {}).get("universalContextSearch") or {}).get("results") or []
        container = results_by_domain[0] if results_by_domain else {}
        items = list(container.get("searchResults") or [])
        next_cursor = container.get("next") or ""

        while len(items) < limit and next_cursor:
            response = await run_persisted_query(
                self._session,
                _OP_DOMAIN_SEARCH_SOCIAL,
                {
                    "searchTerm": query,
                    "searchDomain": "SocialAllAccounts",
                    "displayTitleLocale": "en-US",
                    "pageSize": min(20, limit - len(items)),
                    "pageOffset": len(items),
                    "nextCursor": next_cursor,
                },
                headers=_SEARCH_COMMON_HEADERS,
                check_errors=False,
            )
            container = (response.get("data") or {}).get("universalDomainSearch") or {}
            page_items = container.get("searchResults") or []
            if not page_items:
                break
            items.extend(page_items)
            next_cursor = container.get("next") or ""

        return [_player_search_result(item) for item in items[:limit]]

    async def universal_search_games(
        self, query: str, *, domain: GameSearchDomain = FULL_GAMES_DOMAIN, limit: int = 20
    ) -> list[GameSearchResult]:
        """Run PSN's universal context search, scoped to a store domain.

        The same operation as :meth:`universal_search_players` under a different ``searchContext`` and a
        different persisted hash. Answers "does the PS Store carry a game by this name", which the
        anonymous storefront gateway cannot: it has no search operation, so this needs the caller's own
        PSN token.

        Pages beyond the first through ``metGetDomainSearchResults`` under
        :data:`_OP_DOMAIN_SEARCH_GAME`, which is a *different* persisted hash from the one
        :meth:`universal_search_players` pages with -- the operation is shared, the registered document
        per domain is not. The first page carries far fewer hits than the domain's total (``"GTA"``: 15
        of 32 games), so without this a ``limit`` above the page size would silently under-answer. At
        most :data:`MAX_GAME_SEARCH_PAGES` further pages are fetched, so one search can under-answer but
        can never spend unbounded requests on the caller's token.

        :param query: The search term.
        :param domain: Which store domain to read: full games (the default) or add-ons.
        :param limit: Maximum number of results to return; fewer come back when PSN runs out of hits or
            the page cap is reached first.
        :returns: A list of :class:`~curator.psn.models.GameSearchResult`, empty when PSN returned no
            container for that domain.
        """
        return await self._session.run_with_reauth(lambda: self._universal_search_games(query, domain, limit))

    async def _universal_search_games(self, query: str, domain: GameSearchDomain, limit: int) -> list[GameSearchResult]:
        response = await run_persisted_query(
            self._session,
            _OP_CONTEXT_SEARCH_GAME,
            {"searchTerm": query, "searchContext": GAME_SEARCH_CONTEXT, "displayTitleLocale": "en-US"},
            headers=_SEARCH_COMMON_HEADERS,
            check_errors=False,
        )
        results_by_domain = ((response.get("data") or {}).get("universalContextSearch") or {}).get("results") or []
        container = _domain_container(results_by_domain, domain)
        items = list(container.get("searchResults") or [])
        next_cursor = container.get("next") or ""

        for _page in range(MAX_GAME_SEARCH_PAGES):
            if len(items) >= limit or not next_cursor:
                break
            response = await run_persisted_query(
                self._session,
                _OP_DOMAIN_SEARCH_GAME,
                {
                    "searchTerm": query,
                    "searchDomain": domain,
                    "pageSize": limit - len(items),
                    "pageOffset": len(items),
                    "nextCursor": next_cursor,
                },
                headers=_SEARCH_COMMON_HEADERS,
                check_errors=False,
            )
            container = (response.get("data") or {}).get("universalDomainSearch") or {}
            page_items = container.get("searchResults") or []
            if not page_items:
                break
            items.extend(page_items)
            next_cursor = container.get("next") or ""

        return [_game_search_result(item) for item in items[:limit]]

    async def devices(self) -> list[AccountDevice]:
        """List the consoles/devices registered (activated) to the authenticated account.

        .. note::
           Self-only -- PSN exposes the device list for the authenticated account only.

        :returns: A list of :class:`~curator.psn.models.AccountDevice`.
        """
        return await self._session.run_with_reauth(self._devices)

    async def _devices(self) -> list[AccountDevice]:
        response = (
            await self._session.get(
                _MY_ACCOUNT_URL,
                params={"includeFields": "device,systemData", "platform": "PS5,PS4,PS3,PSVita"},
            )
        ).json()
        devices: list[AccountDevice] = []
        for entry in response.get("accountDevices") or []:
            devices.append(
                AccountDevice(
                    device_id=entry.get("deviceId"),
                    device_type=entry.get("deviceType"),
                    device_name=entry.get("deviceName"),
                    activation_type=entry.get("activationType"),
                    activation_date=entry.get("activationDate"),
                    deactivation_date=entry.get("deactivationDate"),
                )
            )
        return devices

    async def online_id(self, account_id: str) -> str | None:
        """Resolve a PSN account id to its current online id.

        Backs ``curator.profile_routes``'s cross-user identity-card lookup: the viewer's own
        :class:`SocialClient` resolves the profile owner's already-public online id from their
        ``psn_account_id``, without ever touching the owner's own stored token. Thin wrapper over
        :func:`curator.psn._identity.online_id_for`, the same private resolver :meth:`friends`/
        :meth:`blocked`/:meth:`available_to_play`/:meth:`friend_requests` already use internally.

        :param account_id: The target account's PSN account id.
        :returns: The online id, or ``None`` if PSN has none on record for this account.
        """
        return await self._session.run_with_reauth(lambda: _identity.online_id_for(self._session, account_id))

    async def share_link(self) -> ProfileShareLink:
        """Get a shareable link to the authenticated user's PSN profile, plus a QR-code image URL.

        .. note::
           Self-only -- PSN issues the share link for the authenticated account.

        :returns: The :class:`~curator.psn.models.ProfileShareLink`.
        """
        return await self._session.run_with_reauth(self._share_link)

    async def _share_link(self) -> ProfileShareLink:
        account_id = await _identity.own_account_id(self._session)
        data = (await self._session.get(f"{_CPSS_URI}/v1/share/profile/{account_id}")).json()
        return ProfileShareLink(
            share_url=data.get("shareUrl"),
            share_image_url=data.get("shareImageUrl"),
            share_image_url_destination=data.get("shareImageUrlDestination"),
        )


SocialClientFactory = Callable[[str], Coroutine[Any, Any, "SocialClient"]]
"""Builds a raw :class:`SocialClient` (never cached) for a given Identity ``sub``. Requires an existing PSN
link. Backs two call sites: ``curator.devices_routes``'s ``devices()`` call (self-only) and
``curator.profile_routes``'s cross-user ``profile()``/``online_id()`` calls (built from the *viewer's* own
sub, called with the *profile owner's* ``account_id`` -- see that module's docstring). Lives alongside
:class:`SocialClient` (rather than in ``curator.app``, where it's built) so callers can import it without
importing ``curator.app`` -- mirrors ``curator.psn.trophy_client.TrophyClientFactory``. Named
``DevicesClientFactory`` prior to the profile feature, when ``devices()`` was its only caller."""
