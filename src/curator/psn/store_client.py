"""Anonymous PlayStation Store catalog client (``web.np.playstation.com``).

See ``AGENTS/Curator.md`` for why this gateway is distinct from the authenticated one the rest of
:mod:`curator.psn` uses.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("curator")

STORE_GRAPHQL_URL = "https://web.np.playstation.com/api/graphql/v1/op"

CATEGORY_GRID_RETRIEVE_OPERATION = "categoryGridRetrieve"

CATEGORY_GRID_RETRIEVE_HASHES = (
    "9845afc0dbaab4965f6563fffc703f588c8e76792000e8610843b8d3ee9c4c09",
    "4ce7d410a4db2c8b635a48c1dcec375906ff63b19dadd87e073f8fd0c0481d35",
)

INSERTION_IMMUNE_SORT = {"name": "productReleaseDate", "isAscending": True}

CLASSIFICATION_FACET = "storeDisplayClassification"

PRODUCT_GENRES_FACET = "productGenres"

PS4_GAMES_CATEGORY_ID = "44d8bb20-653e-431e-8ad0-c0a365f68d2f"

FULL_GAME_FACET_KEY = "FULL_GAME"

FULL_GAME_FILTER = f"{CLASSIFICATION_FACET}:{FULL_GAME_FACET_KEY}"


class StoreCatalogError(Exception):
    """Raised on a store-gateway response the caller cannot use."""


class StoreQueryRotatedError(StoreCatalogError):
    """Raised when the gateway no longer whitelists the persisted-query hash."""


class StoreFilterIgnoredError(StoreCatalogError):
    """Raised when a requested ``filterBy`` provably did not take effect."""


_COVER_ART_ROLE_PREFERENCE = ("GAMEHUB_COVER_ART", "EDITION_KEY_ART", "PORTRAIT_BANNER", "BACKGROUND")

FULL_GAME_CLASSIFICATION = "Full Game"


@dataclass(frozen=True, slots=True)
class StoreProduct:
    """One product as the storefront lists it."""

    product_id: str
    name: str
    platforms: tuple[str, ...]
    np_title_id: str | None
    cover_image_url: str | None
    classification: str | None

    @property
    def is_full_game(self) -> bool:
        """Whether this is a game rather than an add-on, per the storefront's own classification."""
        return self.classification == FULL_GAME_CLASSIFICATION


@dataclass(frozen=True, slots=True)
class StoreCategoryPage:
    """One page of a category walk. Terminate on :attr:`is_last`, not on :attr:`total_count`."""

    products: tuple[StoreProduct, ...]
    total_count: int
    offset: int
    is_last: bool


class StoreCatalogClient:
    """Reads the public PlayStation Store catalog. Anonymous -- no PSN token, no API key.

    :param client: The HTTP client to call through.
    :param locale: The storefront locale to read, e.g. ``"en-US"``.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        locale: str = "en-US",
        query_hashes: Sequence[str] = CATEGORY_GRID_RETRIEVE_HASHES,
    ) -> None:
        self._client = client
        self._locale = locale
        self._query_hashes = tuple(query_hashes) or CATEGORY_GRID_RETRIEVE_HASHES

    async def category_page(
        self, category_id: str, *, offset: int = 0, size: int = 100, filter_by: Sequence[str] = ()
    ) -> StoreCategoryPage:
        """Fetch one page of a storefront category, trying each configured hash in turn.

        :param category_id: The storefront category id to walk.
        :param offset: How many products to skip.
        :param size: Page size.
        :param filter_by: Storefront facet filters as ``"facetName:FACET_KEY"`` strings, e.g.
            :data:`FULL_GAME_FILTER`. Each is verified against the response's own facet census.
        :raises StoreQueryRotatedError: If *every* configured hash is rejected as no longer whitelisted.
        :raises StoreFilterIgnoredError: If a requested filter did not take effect.
        :raises StoreCatalogError: On any other unusable response.
        """
        grid = await self._retrieve_grid(category_id, offset=offset, size=size, filter_by=tuple(filter_by))
        page_info = grid.get("pageInfo") or {}
        total_count = int(page_info.get("totalCount") or 0)
        _raise_for_ignored_filters(grid, tuple(filter_by), total_count, category_id)
        return StoreCategoryPage(
            products=tuple(_to_product(raw) for raw in (grid.get("products") or []) if raw.get("id")),
            total_count=total_count,
            offset=int(page_info.get("offset", offset)),
            is_last=bool(page_info.get("isLast")),
        )

    async def facet_census(self, category_id: str, facet_name: str) -> dict[str, int] | None:
        """Read one facet's whole published vocabulary and per-key counts in a single page request.

        The census is computed by the gateway over the entire category, not over the page, so ``size=1``
        returns the same census a full page would.

        :param category_id: The storefront category to read.
        :param facet_name: The facet to census, e.g. ``"productGenres"``.
        :returns: ``{facetKey: count}``, or ``None`` if this category publishes no such facet.
        :raises StoreQueryRotatedError: If *every* configured hash is rejected as no longer whitelisted.
        :raises StoreCatalogError: On any other unusable response.
        """
        grid = await self._retrieve_grid(category_id, offset=0, size=1, filter_by=())
        return _facet_census(grid, facet_name)

    async def _retrieve_grid(
        self, category_id: str, *, offset: int, size: int, filter_by: tuple[str, ...]
    ) -> dict[str, Any]:
        rotated: StoreQueryRotatedError | None = None
        for sha256_hash in self._query_hashes:
            try:
                return await self._request_grid(
                    category_id, offset=offset, size=size, sha256_hash=sha256_hash, filter_by=filter_by
                )
            except StoreQueryRotatedError as error:
                rotated = error
                logger.warning("Store persisted-query hash %s rejected; trying the next candidate", sha256_hash[:12])

        raise rotated or StoreQueryRotatedError("No persisted-query hash is configured for categoryGridRetrieve.")

    async def _request_grid(
        self, category_id: str, *, offset: int, size: int, sha256_hash: str, filter_by: tuple[str, ...]
    ) -> dict[str, Any]:
        operation_name = CATEGORY_GRID_RETRIEVE_OPERATION
        params = {
            "operationName": operation_name,
            "variables": json.dumps(
                {
                    "id": category_id,
                    "pageArgs": {"size": size, "offset": offset},
                    "sortBy": INSERTION_IMMUNE_SORT,
                    "filterBy": list(filter_by),
                    "facetOptions": [],
                }
            ),
            "extensions": json.dumps({"persistedQuery": {"version": 1, "sha256Hash": sha256_hash}}),
        }
        headers = {
            "x-psn-store-locale-override": self._locale,
            "apollo-require-preflight": "true",
            "x-apollo-operation-name": operation_name,
        }

        response = await self._client.get(STORE_GRAPHQL_URL, params=params, headers=headers)
        if response.status_code >= 500:
            raise StoreCatalogError(f"PlayStation Store returned {response.status_code}.")

        payload: dict[str, Any] = response.json()
        _raise_for_store_errors(payload, operation_name)

        grid: dict[str, Any] = (payload.get("data") or {}).get("categoryGridRetrieve") or {}
        return grid


def _facet_census(grid: dict[str, Any], facet_name: str) -> dict[str, int] | None:
    """Return ``{facetKey: count}`` for one facet, or ``None`` if this category doesn't publish it."""
    for facet in grid.get("facetOptions") or []:
        if facet.get("name") == facet_name:
            return {
                str(value.get("key")): int(value.get("count") or 0)
                for value in facet.get("values") or []
                if value.get("key") is not None
            }
    return None


def _raise_for_ignored_filters(
    grid: dict[str, Any], filter_by: tuple[str, ...], total_count: int, category_id: str
) -> None:
    """Verify each requested filter narrowed the result to that facet's own count.

    A category publishing no census for the facet is allowed through unchecked.
    """
    for filter_expression in filter_by:
        facet_name, _, facet_key = filter_expression.partition(":")
        census = _facet_census(grid, facet_name)
        if census is None:
            continue

        if facet_key not in census:
            raise StoreFilterIgnoredError(
                f"PlayStation Store cannot honour filter '{filter_expression}' on category {category_id}: "
                f"that category publishes no '{facet_key}' value for facet '{facet_name}' (it offers "
                f"{sorted(census)}). Filtering on it returns nothing, which would otherwise end the walk "
                f"immediately and look like success."
            )

        expected = census[facet_key]
        if expected != total_count:
            raise StoreFilterIgnoredError(
                f"PlayStation Store ignored filter '{filter_expression}' on category {category_id}: the "
                f"category reports {expected} products for that facet but the filtered query returned "
                f"{total_count}. The gateway answers 200 for a filter it does not honour, so this is "
                f"checked rather than trusted."
            )


def _raise_for_store_errors(payload: dict[str, Any], operation_name: str) -> None:
    """Translate the gateway's two distinct failure shapes into typed errors."""
    message = payload.get("message")
    if isinstance(message, str) and "not whitelisted" in message.lower():
        raise StoreQueryRotatedError(
            f"The persisted-query hash for '{operation_name}' is no longer whitelisted by the PlayStation "
            f"Store; refresh it from the store site's own network traffic and update this client. "
            f"Gateway said: {message.strip()}"
        )

    errors = payload.get("errors")
    if errors:
        detail = str(errors[0].get("message", "unknown error")).strip()
        raise StoreCatalogError(f"PlayStation Store '{operation_name}' failed: {detail}")


def _cover_image_url(media: list[dict[str, Any]]) -> str | None:
    by_role = {str(item.get("role")): str(item.get("url")) for item in media if item.get("type") == "IMAGE"}
    for role in _COVER_ART_ROLE_PREFERENCE:
        if role in by_role:
            return by_role[role]
    return None


def _to_product(raw: dict[str, Any]) -> StoreProduct:
    return StoreProduct(
        product_id=str(raw["id"]),
        name=str(raw.get("name") or ""),
        platforms=tuple(str(platform) for platform in (raw.get("platforms") or [])),
        np_title_id=str(raw["npTitleId"]) if raw.get("npTitleId") else None,
        cover_image_url=_cover_image_url(raw.get("media") or []),
        classification=str(raw["localizedStoreDisplayClassification"])
        if raw.get("localizedStoreDisplayClassification")
        else None,
    )
