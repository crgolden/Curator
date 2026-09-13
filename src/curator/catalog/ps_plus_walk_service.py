"""Walks the PS Plus catalog categories and records membership with a lifecycle.

Unlike the catalog backfill, nothing here creates ``games`` and nothing filters to full games: bundles
and premium editions are members too, and ``classification`` records what each is. The decisions behind
the walk are in ``AGENTS/REPOS/Curator.md``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol

from curator.catalog.ps_plus_repository import PsPlusCategory, PsPlusWalkWriter
from curator.catalog.store_backfill_service import DEFAULT_PAGE_DELAY_SECONDS, PAGE_SIZE, next_page_offset
from curator.psn.store_client import StoreCatalogClient, StoreFilterIgnoredError, StoreQueryRotatedError

CATEGORY_RENAMED = "category_renamed"
QUERY_ROTATED = "query_rotated"
FILTER_NOT_APPLIED = "filter_not_applied"
NO_PRODUCTS = "no_products"
PAGE_BUDGET_EXHAUSTED = "page_budget_exhausted"


class PsPlusWalkStore(Protocol):
    """The slice of :class:`~curator.catalog.ps_plus_repository.PsPlusRepository` the walk needs."""

    async def list_categories(self) -> Sequence[PsPlusCategory]: ...

    def walk(self, category_id: str) -> AbstractAsyncContextManager[PsPlusWalkWriter]: ...


@dataclass(frozen=True, slots=True)
class PsPlusWalkProgress:
    """What one category's walk achieved.

    :param completed: The walk reached the category's last page. Only a completed walk with no
        :attr:`coverage_shortfall` marks departures.
    """

    category_id: str
    tier: str
    walk_id: str
    pages_read: int
    distinct_products: int
    reported_total: int | None
    stopped_reason: str | None
    completed: bool

    @property
    def coverage_shortfall(self) -> int:
        """How many products a completed walk never saw; zero for a walk that stopped short."""
        if self.reported_total is None or not self.completed:
            return 0
        return max(self.reported_total - self.distinct_products, 0)


class PsPlusWalkService:
    """Walks each configured category into ``ps_plus_catalog_memberships``.

    :param client: The anonymous storefront client.
    :param repository: Where categories are read and memberships written.
    :param page_delay_seconds: Pacing between page requests.
    :param clock: Supplies the instants stamped on walks and memberships.
    """

    def __init__(
        self,
        client: StoreCatalogClient,
        repository: PsPlusWalkStore,
        *,
        page_delay_seconds: float = DEFAULT_PAGE_DELAY_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._repository = repository
        self._page_delay_seconds = page_delay_seconds
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def walk_all(self, *, max_pages_per_category: int | None = None) -> list[PsPlusWalkProgress]:
        """Walk every configured category in turn, stopping early when the persisted query has rotated.

        :param max_pages_per_category: Stop each category after this many pages.
        """
        results: list[PsPlusWalkProgress] = []
        for category in await self._repository.list_categories():
            progress = await self.walk_category(category, max_pages=max_pages_per_category)
            results.append(progress)
            if progress.stopped_reason == QUERY_ROTATED:
                break
        return results

    async def walk_category(self, category: PsPlusCategory, *, max_pages: int | None = None) -> PsPlusWalkProgress:
        """Walk one category under its advisory lock, writing memberships page by page.

        The first page's ``reportingName`` must start with the category's stored prefix; otherwise the walk
        stops as ``category_renamed`` and writes no membership. Departures are marked only when the walk
        completes with no coverage shortfall.

        :param category: The category to walk.
        :param max_pages: Stop after this many pages.
        """
        async with self._repository.walk(category.category_id) as writer:
            walk_id = await writer.begin(self._clock())
            return await self._walk_pages(category, writer, walk_id, max_pages)

    async def _walk_pages(
        self, category: PsPlusCategory, writer: PsPlusWalkWriter, walk_id: str, max_pages: int | None
    ) -> PsPlusWalkProgress:
        offset = 0
        pages_read = 0
        seen_product_ids: set[str] = set()
        reported_total: int | None = None

        while max_pages is None or pages_read < max_pages:
            try:
                page = await self._client.category_page(category.category_id, offset=offset, size=PAGE_SIZE)
            except StoreQueryRotatedError:
                await writer.stop(walk_id, QUERY_ROTATED, reported_total, len(seen_product_ids))
                return self._progress(category, walk_id, pages_read, seen_product_ids, reported_total, QUERY_ROTATED)
            except StoreFilterIgnoredError:
                await writer.stop(walk_id, FILTER_NOT_APPLIED, reported_total, len(seen_product_ids))
                return self._progress(
                    category, walk_id, pages_read, seen_product_ids, reported_total, FILTER_NOT_APPLIED
                )

            if pages_read == 0 and not _reporting_name_matches(page.reporting_name, category.reporting_name_prefix):
                await writer.stop(walk_id, CATEGORY_RENAMED, page.total_count, 0)
                return self._progress(category, walk_id, 1, set(), page.total_count, CATEGORY_RENAMED)

            pages_read += 1
            reported_total = page.total_count
            seen_product_ids.update(product.product_id for product in page.products)
            if page.products:
                await writer.record_products(walk_id, page.products, self._clock())

            offset = next_page_offset(page, offset)
            if page.is_last or not page.products:
                if not seen_product_ids:
                    await writer.stop(walk_id, NO_PRODUCTS, reported_total, 0)
                    return self._progress(category, walk_id, pages_read, seen_product_ids, reported_total, NO_PRODUCTS)
                completed_at = self._clock()
                await writer.complete(walk_id, completed_at, reported_total, len(seen_product_ids))
                progress = self._progress(
                    category, walk_id, pages_read, seen_product_ids, reported_total, None, completed=True
                )
                if progress.coverage_shortfall == 0:
                    await writer.mark_departures(walk_id, completed_at)
                return progress

            if self._page_delay_seconds:
                await asyncio.sleep(self._page_delay_seconds)

        await writer.stop(walk_id, PAGE_BUDGET_EXHAUSTED, reported_total, len(seen_product_ids))
        return self._progress(category, walk_id, pages_read, seen_product_ids, reported_total, PAGE_BUDGET_EXHAUSTED)

    @staticmethod
    def _progress(
        category: PsPlusCategory,
        walk_id: str,
        pages_read: int,
        seen_product_ids: set[str],
        reported_total: int | None,
        stopped_reason: str | None,
        *,
        completed: bool = False,
    ) -> PsPlusWalkProgress:
        return PsPlusWalkProgress(
            category_id=category.category_id,
            tier=category.tier,
            walk_id=walk_id,
            pages_read=pages_read,
            distinct_products=len(seen_product_ids),
            reported_total=reported_total,
            stopped_reason=stopped_reason,
            completed=completed,
        )


def _reporting_name_matches(reporting_name: str | None, prefix: str) -> bool:
    return reporting_name is not None and reporting_name.startswith(prefix)
