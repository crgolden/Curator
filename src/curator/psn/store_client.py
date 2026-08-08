"""Anonymous PlayStation Store catalog client (``web.np.playstation.com``).

See ``AGENTS/Curator.md`` for why this gateway is distinct from the authenticated one the rest of
:mod:`curator.psn` uses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

STORE_GRAPHQL_URL = "https://web.np.playstation.com/api/graphql/v1/op"

CATEGORY_GRID_RETRIEVE = (
    "categoryGridRetrieve",
    "9845afc0dbaab4965f6563fffc703f588c8e76792000e8610843b8d3ee9c4c09",
)


class StoreCatalogError(Exception):
    """Raised on a store-gateway response the caller cannot use."""


class StoreQueryRotatedError(StoreCatalogError):
    """Raised when the gateway no longer whitelists the persisted-query hash."""


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

    def __init__(self, client: httpx.AsyncClient, *, locale: str = "en-US") -> None:
        self._client = client
        self._locale = locale

    async def category_page(self, category_id: str, *, offset: int = 0, size: int = 100) -> StoreCategoryPage:
        """Fetch one page of a storefront category.

        :param category_id: The storefront category id to walk.
        :param offset: How many products to skip.
        :param size: Page size.
        :raises StoreQueryRotatedError: If the persisted-query hash is no longer whitelisted.
        :raises StoreCatalogError: On any other unusable response.
        """
        operation_name, sha256_hash = CATEGORY_GRID_RETRIEVE
        params = {
            "operationName": operation_name,
            "variables": json.dumps(
                {
                    "id": category_id,
                    "pageArgs": {"size": size, "offset": offset},
                    "sortBy": None,
                    "filterBy": [],
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

        grid = (payload.get("data") or {}).get("categoryGridRetrieve") or {}
        page_info = grid.get("pageInfo") or {}
        return StoreCategoryPage(
            products=tuple(_to_product(raw) for raw in (grid.get("products") or []) if raw.get("id")),
            total_count=int(page_info.get("totalCount") or 0),
            offset=int(page_info.get("offset", offset)),
            is_last=bool(page_info.get("isLast")),
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
