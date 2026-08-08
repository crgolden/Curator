"""Walks the public PlayStation Store and seeds the shared catalog from it.

Design rationale and the decisions behind the walk are in ``AGENTS/DESIGNS.md`` (§7 item 4).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from curator.psn.store_client import (
    StoreCatalogClient,
    StoreCategoryPage,
    StoreProduct,
    StoreQueryRotatedError,
)

logger = logging.getLogger("curator")

PAGE_SIZE = 100

DEFAULT_PAGE_DELAY_SECONDS = 0.5


class CatalogBackfillWriter(Protocol):
    """The slice of :class:`~curator.catalog.repository.CatalogRepository` this service needs."""

    async def backfill_store_products(self, products: Sequence[StoreProduct]) -> tuple[int, int]: ...


@dataclass(frozen=True, slots=True)
class BackfillProgress:
    """Where a walk stopped, and what it achieved."""

    category_id: str
    next_offset: int
    completed: bool
    pages_read: int = 0
    products_seen: int = 0
    games_created: int = 0
    covers_cached: int = 0
    stopped_reason: str | None = None


@dataclass
class BackfillSummary:
    """Aggregate result across every category in one run."""

    categories: list[BackfillProgress] = field(default_factory=list)

    @property
    def games_created(self) -> int:
        return sum(progress.games_created for progress in self.categories)

    @property
    def covers_cached(self) -> int:
        return sum(progress.covers_cached for progress in self.categories)

    @property
    def completed(self) -> bool:
        return all(progress.completed for progress in self.categories)


class StoreBackfillService:
    """Pages storefront categories into the shared catalog.

    :param client: The anonymous storefront client.
    :param repository: Where resolved products are written.
    :param page_delay_seconds: Pacing between page requests.
    """

    def __init__(
        self,
        client: StoreCatalogClient,
        repository: CatalogBackfillWriter,
        *,
        page_delay_seconds: float = DEFAULT_PAGE_DELAY_SECONDS,
    ) -> None:
        self._client = client
        self._repository = repository
        self._page_delay_seconds = page_delay_seconds

    async def backfill_category(
        self, category_id: str, *, start_offset: int = 0, max_pages: int | None = None
    ) -> BackfillProgress:
        """Walk one category from ``start_offset``, writing each page as it is read.

        :param category_id: The storefront category to walk.
        :param start_offset: Where to resume from.
        :param max_pages: Stop after this many pages.
        :returns: A :class:`BackfillProgress` whose ``next_offset`` resumes exactly here.
        """
        offset = start_offset
        pages_read = 0
        products_seen = 0
        games_created = 0
        covers_cached = 0

        while max_pages is None or pages_read < max_pages:
            try:
                page = await self._client.category_page(category_id, offset=offset, size=PAGE_SIZE)
            except StoreQueryRotatedError:
                logger.exception("Store backfill halted: persisted-query hash rejected")
                return self._progress(
                    category_id, offset, False, pages_read, products_seen, games_created, covers_cached, "query_rotated"
                )

            pages_read += 1
            products_seen += len(page.products)

            full_games = [product for product in page.products if product.is_full_game]
            if full_games:
                created, cached = await self._repository.backfill_store_products(full_games)
                games_created += created
                covers_cached += cached

            offset = self._next_offset(page, offset)
            if page.is_last or not page.products:
                return self._progress(
                    category_id, offset, True, pages_read, products_seen, games_created, covers_cached, None
                )

            if self._page_delay_seconds:
                await asyncio.sleep(self._page_delay_seconds)

        return self._progress(
            category_id, offset, False, pages_read, products_seen, games_created, covers_cached, "page_budget_exhausted"
        )

    async def backfill(
        self, category_ids: Sequence[str], *, max_pages_per_category: int | None = None
    ) -> BackfillSummary:
        """Walk several categories in sequence, one at a time."""
        summary = BackfillSummary()
        for category_id in category_ids:
            progress = await self.backfill_category(category_id, max_pages=max_pages_per_category)
            summary.categories.append(progress)
            if progress.stopped_reason == "query_rotated":
                break
        return summary

    @staticmethod
    def _next_offset(page: StoreCategoryPage, requested_offset: int) -> int:
        """Advance past the page just read, by the count actually returned."""
        return (page.offset if page.offset >= requested_offset else requested_offset) + max(len(page.products), 1)

    @staticmethod
    def _progress(
        category_id: str,
        next_offset: int,
        completed: bool,
        pages_read: int,
        products_seen: int,
        games_created: int,
        covers_cached: int,
        stopped_reason: str | None,
    ) -> BackfillProgress:
        return BackfillProgress(
            category_id=category_id,
            next_offset=next_offset,
            completed=completed,
            pages_read=pages_read,
            products_seen=products_seen,
            games_created=games_created,
            covers_cached=covers_cached,
            stopped_reason=stopped_reason,
        )
