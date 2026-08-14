"""Async client for PSN Store catalog data: title concepts and universal game search.

``title_concept()`` is a genuinely new capability the legacy curation pipeline never had -- official,
first-party PSN Store catalog data (genres, star rating, publisher, release date, cover image), more
reliable than the legacy pipeline's SSR-HTML scrape of the public PS Store (subject to IP-based 403 blocks
after ~200 requests). ``curator.enrichment.enrichment_service`` uses it as the primary PSN-side enrichment
signal.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine, Sequence
from datetime import date, datetime
from typing import Any, Protocol, TypeVar

from curator.psn._graphql import run_persisted_query
from curator.psn.errors import PsnAuthError
from curator.psn.models import GameSearchResult, TitleConcept
from curator.psn.session import PsnSession

logger = logging.getLogger("curator")

_T = TypeVar("_T")

_GAME_TITLES_URI = "https://m.np.playstation.com/api/catalog/v2/titles"

_SEARCH_COMMON_HEADERS = {
    "accept": "application/json",
    "content-type": "application/json",
    "apollographql-client-name": "PlayStationApp-Android",
    "apollographql-client-version": "25.4.0",
}
_OP_CONTEXT_SEARCH_GAMES = (
    "metGetContextSearchResults",
    "a2fbc15433b37ca7bfcd7112f741735e13268f5e9ebd5ffce51b85acc126f41d",
)
_OP_DOMAIN_SEARCH_GAMES = (
    "metGetDomainSearchResults",
    "b51624299bd17b3799f77c9f097cc8887a04d3873f0329095976a841595bc902",
)


def _opt_dict(value: Any) -> dict[str, Any]:
    """Return ``value`` if it's a dict, else ``{}`` -- so ``.get()`` on a missing/malformed field is safe."""
    return value if isinstance(value, dict) else {}


def _to_float(value: Any) -> float | None:
    """Coerce a PSN numeric field to float. PSN returns some numbers (e.g. star rating) as strings."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _cover_image_url(media: Any) -> str | None:
    """Pick the best cover-art URL from a concept's ``media.images`` list.

    PSN returns several image types per title; we prefer the store cover, then the master art.
    """
    images = (media or {}).get("images") if isinstance(media, dict) else None
    if not images:
        return None
    by_type = {img.get("type"): img.get("url") for img in images if isinstance(img, dict)}
    for preferred in ("GAMEHUB_COVER_ART", "MASTER", "LOGO"):
        if by_type.get(preferred):
            return str(by_type[preferred])
    return next((img.get("url") for img in images if isinstance(img, dict) and img.get("url")), None)


_LOCAL_PLAYER_COUNT_NOTICE = "NO_OF_PLAYERS"

_ONLINE_PLAYER_COUNT_NOTICES = ("NO_OF_NETWORK_PLAYERS", "NO_OF_NETWORK_PLAYERS_PS_PLUS")


def _multiplayer(concept: dict[str, Any]) -> bool | None:
    """Whether PSN's compatibility notices state a player count above one, or ``None`` if it states none."""
    counts: list[int] = []
    for notice in concept.get("compatibilityNotices") or []:
        if not isinstance(notice, dict):
            continue
        if notice.get("type") in (_LOCAL_PLAYER_COUNT_NOTICE, *_ONLINE_PLAYER_COUNT_NOTICES):
            value = str(notice.get("value") or "")
            if value.isdigit():
                counts.append(int(value))
    if not counts:
        return None
    return max(counts) > 1


def _release_date(value: Any) -> date | None:
    """Read PSN's release date as a ``date``, dropping the time, or ``None`` when it is absent or
    unparseable."""
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _parse_title_concept(concept: dict[str, Any]) -> TitleConcept:
    """Map a raw PSN store concept payload (from :meth:`CatalogClient.title_concept`) to :class:`TitleConcept`."""
    release = _opt_dict(concept.get("releaseDate"))
    rating = _opt_dict(concept.get("contentRating"))
    star = _opt_dict(concept.get("starRating"))
    concept_id = concept.get("id")
    return TitleConcept(
        concept_id=str(concept_id) if concept_id is not None else None,
        name=concept.get("name"),
        type=concept.get("type"),
        publisher=concept.get("publisherName"),
        release_date=_release_date(release.get("date")),
        minimum_age=concept.get("minimumAge"),
        content_rating=rating.get("name"),
        rating_authority=rating.get("authority"),
        star_rating=_to_float(star.get("score")),
        genres=tuple(concept.get("genres") or ()),
        title_ids=tuple(concept.get("titleIds") or ()),
        cover_image_url=_cover_image_url(concept.get("media")),
        multiplayer=_multiplayer(concept),
    )


def _game_search_result(item: dict[str, Any]) -> GameSearchResult:
    """Map a raw PSN universal-search game/add-on item to our :class:`GameSearchResult`."""
    result = item.get("result") or {}
    price = result.get("price") or (result.get("defaultProduct") or {}).get("price") or {}
    media = result.get("media") or []
    image_url = next((m.get("url") for m in media if isinstance(m, dict) and m.get("url")), None)
    return GameSearchResult(
        id=result.get("id") or item.get("id"),
        name=result.get("name"),
        type=result.get("type") or result.get("itemType"),
        platforms=tuple(result.get("platforms") or ()),
        image_url=image_url,
        price=price.get("basePrice") if isinstance(price, dict) else None,
        discounted_price=price.get("discountedPrice") if isinstance(price, dict) else None,
        is_free=price.get("isFree") if isinstance(price, dict) else None,
    )


class CatalogClient:
    """PSN Store catalog operations: title concepts, universal game search.

    :param session: The authenticated :class:`~curator.psn.session.PsnSession` to call through.
    """

    def __init__(self, session: PsnSession) -> None:
        self._session = session

    async def title_concept(self, title_id: str, platform: str = "PS5") -> TitleConcept:
        """Get public store/catalog details for a game title (its concept): name, publisher, genres, rating.

        This is catalog metadata -- it does not require owning the title.

        :param title_id: The title's npTitleId (e.g. ``"CUSA00419_00"``).
        :param platform: Unused -- the concept endpoint carries no platform-specific data. Kept for API stability.
        :returns: The :class:`~curator.psn.models.TitleConcept`.
        """
        return await self._session.run_with_reauth(lambda: self._title_concept(title_id))

    async def _title_concept(self, title_id: str) -> TitleConcept:
        details = (
            await self._session.get(
                f"{_GAME_TITLES_URI}/{title_id}/concepts",
                params={"age": 99, "country": "US", "language": "en-US"},
            )
        ).json()
        concept = details[0] if details else {}
        return _parse_title_concept(concept)

    async def search_games(self, query: str, addons: bool = False, limit: int = 20) -> list[GameSearchResult]:
        """Search the PlayStation Store for games (or add-ons) by name.

        :param query: The search term.
        :param addons: Search add-ons/DLC instead of full games.
        :param limit: Maximum number of results to return.
        :returns: A list of :class:`~curator.psn.models.GameSearchResult`.
        """
        return await self._session.run_with_reauth(lambda: self._search_games(query, addons, limit))

    async def _search_games(self, query: str, addons: bool, limit: int) -> list[GameSearchResult]:
        domain_index = 1 if addons else 0
        response = await run_persisted_query(
            self._session,
            _OP_CONTEXT_SEARCH_GAMES,
            {"searchTerm": query, "searchContext": "MobileUniversalSearchGame", "displayTitleLocale": "en-US"},
            headers=_SEARCH_COMMON_HEADERS,
            check_errors=False,
        )
        results_by_domain = ((response.get("data") or {}).get("universalContextSearch") or {}).get("results") or []
        container = results_by_domain[domain_index] if len(results_by_domain) > domain_index else {}
        items = list(container.get("searchResults") or [])
        next_cursor = container.get("next") or ""

        while len(items) < limit and next_cursor:
            response = await run_persisted_query(
                self._session,
                _OP_DOMAIN_SEARCH_GAMES,
                {
                    "searchTerm": query,
                    "searchDomain": "MobileAddOns" if addons else "MobileGames",
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

        return [_game_search_result(item) for item in items[:limit]]


class CatalogClientProtocol(Protocol):
    """The subset of :class:`CatalogClient` that
    :class:`~curator.enrichment.enrichment_service.EnrichmentService` depends on -- satisfied structurally
    by both :class:`CatalogClient` and :class:`RotatingCatalogClient`."""

    async def title_concept(self, title_id: str, platform: str = "PS5") -> TitleConcept: ...


class InMemoryTokenStore:
    """A :class:`~curator.psn.session.TokenStore` that keeps the derived token in the process only."""

    def __init__(self) -> None:
        self._token: dict[str, Any] | None = None

    async def load(self) -> dict[str, Any] | None:
        """Return the token held for this process, or ``None`` before the first exchange."""
        return self._token

    async def save(self, token_response: dict[str, Any]) -> None:
        """Hold ``token_response`` for the life of this object."""
        self._token = token_response

    async def clear(self) -> None:
        """Drop the held token."""
        self._token = None


class RotatingCatalogClient:
    """Rotates across multiple single-account :class:`CatalogClient` instances behind the same surface
    :class:`~curator.enrichment.enrichment_service.EnrichmentService` calls, advancing to the next
    account when the current one's credential is rejected.

    Rotates on :class:`~curator.psn.errors.PsnAuthError`.

    :param clients: One client per configured npsso, in rotation order.
    :raises ValueError: If ``clients`` is empty.
    """

    def __init__(self, clients: Sequence[CatalogClientProtocol]) -> None:
        if not clients:
            raise ValueError("RotatingCatalogClient requires at least one client.")
        self._clients = clients
        self._index = 0

    async def title_concept(self, title_id: str, platform: str = "PS5") -> TitleConcept:
        """Resolve a title's concept through whichever configured account is currently working."""
        return await self._call(lambda client: client.title_concept(title_id, platform))

    async def _call(self, invoke: Callable[[CatalogClientProtocol], Coroutine[Any, Any, _T]]) -> _T:
        """Try the current account, rotating on a rejected credential and retrying the same call, until
        one succeeds or every account has failed once -- then re-raise the last error, leaving the index
        where it landed rather than resetting."""
        rejections: list[PsnAuthError] = []
        for _ in range(len(self._clients)):
            try:
                return await invoke(self._clients[self._index])
            except PsnAuthError as error:
                rejections.append(error)
                logger.warning("PSN account %d of %d rejected; rotating.", self._index + 1, len(self._clients))
                self._index = (self._index + 1) % len(self._clients)
        raise rejections[-1]
