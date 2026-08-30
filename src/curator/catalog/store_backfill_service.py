"""Walks the public PlayStation Store and seeds the shared catalog from it.

Design rationale and the decisions behind the walk are in ``AGENTS/DESIGNS.md`` (§7 item 4).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from curator.psn.store_client import (
    FULL_GAME_FILTER,
    StoreCatalogClient,
    StoreCategoryPage,
    StoreFilterIgnoredError,
    StoreProduct,
    StoreQueryRotatedError,
)

logger = logging.getLogger("curator")

PAGE_SIZE = 100

DEFAULT_PAGE_DELAY_SECONDS = 0.5

_CATEGORY_INDEPENDENT_STOPS = frozenset({"query_rotated", "filter_not_applied"})


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
    distinct_products: int = 0
    reported_total: int | None = None
    start_offset: int = 0

    @property
    def coverage_shortfall(self) -> int:
        """How many products a completed walk never saw, over the span it actually covered.

        A walk resumed from ``start_offset`` is only accountable for what lies beyond it, not for the
        products an earlier run already read.
        """
        if self.reported_total is None or not self.completed:
            return 0
        expected = max(self.reported_total - self.start_offset, 0)
        return max(expected - self.distinct_products, 0)


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
        """Walk one category's full games from ``start_offset``, writing each page as it is read.

        Filtered to :data:`~curator.psn.store_client.FULL_GAME_FILTER` where the category supports it, so
        ``reported_total`` and ``coverage_shortfall`` count full games rather than the whole category.

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
        seen_product_ids: set[str] = set()
        reported_total: int | None = None

        while max_pages is None or pages_read < max_pages:
            try:
                page = await self._client.category_page(
                    category_id, offset=offset, size=PAGE_SIZE, filter_by=(FULL_GAME_FILTER,)
                )
            except StoreQueryRotatedError:
                logger.exception("Store backfill halted: every persisted-query hash rejected")
                return self._progress(
                    category_id,
                    offset,
                    False,
                    pages_read,
                    products_seen,
                    games_created,
                    covers_cached,
                    "query_rotated",
                    seen_product_ids,
                    reported_total,
                    start_offset,
                )
            except StoreFilterIgnoredError:
                logger.exception("Store backfill halted: the full-game filter was not honoured")
                return self._progress(
                    category_id,
                    offset,
                    False,
                    pages_read,
                    products_seen,
                    games_created,
                    covers_cached,
                    "filter_not_applied",
                    seen_product_ids,
                    reported_total,
                    start_offset,
                )

            pages_read += 1
            products_seen += len(page.products)
            reported_total = page.total_count
            seen_product_ids.update(product.product_id for product in page.products)

            full_games = [product for product in page.products if product.is_full_game]
            if full_games:
                created, cached = await self._repository.backfill_store_products(full_games)
                games_created += created
                covers_cached += cached

            offset = self._next_offset(page, offset)
            if page.is_last or not page.products:
                walked_the_whole_category_and_found_nothing = products_seen == 0 and start_offset == 0
                progress = self._progress(
                    category_id,
                    offset,
                    not walked_the_whole_category_and_found_nothing,
                    pages_read,
                    products_seen,
                    games_created,
                    covers_cached,
                    "no_products" if walked_the_whole_category_and_found_nothing else None,
                    seen_product_ids,
                    reported_total,
                    start_offset,
                )
                if walked_the_whole_category_and_found_nothing:
                    logger.warning(
                        "Store backfill of category %s read a first page containing no products at all. "
                        "The storefront answered, so this is not a transport failure -- the id is most "
                        "likely not a category, or is one that publishes nothing.",
                        category_id,
                    )
                if progress.coverage_shortfall:
                    logger.warning(
                        "Store backfill of category %s saw %d of %d products; %d were missed because the "
                        "category changed mid-walk. Re-run to pick them up.",
                        category_id,
                        progress.distinct_products,
                        progress.reported_total,
                        progress.coverage_shortfall,
                    )
                return progress

            if self._page_delay_seconds:
                await asyncio.sleep(self._page_delay_seconds)

        return self._progress(
            category_id,
            offset,
            False,
            pages_read,
            products_seen,
            games_created,
            covers_cached,
            "page_budget_exhausted",
            seen_product_ids,
            reported_total,
            start_offset,
        )

    async def backfill(
        self,
        category_ids: Sequence[str],
        *,
        max_pages_per_category: int | None = None,
        start_offsets: Mapping[str, int] | None = None,
    ) -> BackfillSummary:
        """Walk several categories in sequence, one at a time, stopping early on a category-independent
        failure.

        :param start_offsets: Per-category offset to resume from, keyed by category id; absent categories
            start at 0. Hand back the ``next_offset`` a previous run reported for that category.
        """
        offsets = start_offsets or {}
        summary = BackfillSummary()
        for category_id in category_ids:
            progress = await self.backfill_category(
                category_id, start_offset=offsets.get(category_id, 0), max_pages=max_pages_per_category
            )
            summary.categories.append(progress)
            if progress.stopped_reason in _CATEGORY_INDEPENDENT_STOPS:
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
        seen_product_ids: set[str],
        reported_total: int | None,
        start_offset: int,
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
            distinct_products=len(seen_product_ids),
            reported_total=reported_total,
            start_offset=start_offset,
        )
