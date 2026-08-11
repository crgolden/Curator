"""Tests for the PlayStation Store catalog backfill walk, using hand-written fakes."""

from __future__ import annotations

from curator.catalog.store_backfill_service import StoreBackfillService
from curator.psn.store_client import StoreCategoryPage, StoreProduct, StoreQueryRotatedError


def product(product_id="P1", name="Bloodborne", np_title_id="CUSA00207_00", cover="cover.jpg", cls="Full Game"):
    return StoreProduct(
        product_id=product_id,
        name=name,
        platforms=("PS4",),
        np_title_id=np_title_id,
        cover_image_url=cover,
        classification=cls,
    )


class FakeStoreClient:
    def __init__(self, pages, error=None):
        self._pages = list(pages)
        self._error = error
        self.calls: list[tuple[str, int, int]] = []

    async def category_page(self, category_id, *, offset=0, size=100):
        self.calls.append((category_id, offset, size))
        if self._error is not None:
            raise self._error
        if not self._pages:
            return StoreCategoryPage(products=(), total_count=0, offset=offset, is_last=True)
        return self._pages.pop(0)


class FakeCatalogRepository:
    def __init__(self):
        self.written: list[list[StoreProduct]] = []

    async def backfill_store_products(self, products):
        self.written.append(list(products))
        return len(products), sum(1 for p in products if p.cover_image_url)


def page(products, *, offset=0, is_last=False, total=1000):
    return StoreCategoryPage(products=tuple(products), total_count=total, offset=offset, is_last=is_last)


def _service(client, repository):
    return StoreBackfillService(client, repository, page_delay_seconds=0)


async def test_a_delisting_mid_walk_is_measured_against_the_final_reported_total():
    client = FakeStoreClient(
        [
            page([product("P1"), product("P2")], offset=0, total=4),
            page([product("P4")], offset=2, total=3, is_last=True),
        ]
    )
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1")

    assert progress.completed
    assert progress.distinct_products == 3
    assert progress.reported_total == 3
    assert progress.coverage_shortfall == 0, "the final total is what the walk is measured against"


async def test_a_short_walk_against_a_larger_total_reports_the_gap():
    client = FakeStoreClient([page([product("P1"), product("P2")], offset=0, total=5, is_last=True)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1")

    assert progress.completed
    assert progress.distinct_products == 2
    assert progress.coverage_shortfall == 3


async def test_repeated_products_across_pages_are_not_double_counted_as_coverage():
    client = FakeStoreClient(
        [
            page([product("P1"), product("P2")], offset=0, total=3),
            page([product("P2"), product("P3")], offset=2, total=3, is_last=True),
        ]
    )
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1")

    assert progress.products_seen == 4, "raw count still reflects what was fetched"
    assert progress.distinct_products == 3
    assert progress.coverage_shortfall == 0


async def test_a_walk_stopped_by_its_page_budget_reports_no_shortfall():
    client = FakeStoreClient([page([product("P1")], offset=0, total=500)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1", max_pages=1)

    assert not progress.completed
    assert progress.stopped_reason == "page_budget_exhausted"
    assert progress.coverage_shortfall == 0


async def test_walks_until_the_gateway_says_it_is_the_last_page():
    client = FakeStoreClient(
        [
            page([product("P1")], offset=0),
            page([product("P2")], offset=1),
            page([product("P3")], offset=2, is_last=True),
        ]
    )
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1")

    assert progress.completed is True
    assert progress.pages_read == 3
    assert progress.products_seen == 3


async def test_terminates_on_is_last_rather_than_on_a_drifting_total_count():
    client = FakeStoreClient([page([product("P1")], offset=0, is_last=True, total=99999)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1")

    assert progress.completed is True, "a totalCount that moves between calls must not drive termination"
    assert progress.pages_read == 1


async def test_writes_each_page_as_it_is_read_so_an_interrupted_walk_still_seeds():
    client = FakeStoreClient([page([product("P1")], offset=0), page([product("P2")], offset=1, is_last=True)])
    repository = FakeCatalogRepository()

    await _service(client, repository).backfill_category("cat-1")

    assert len(repository.written) == 2, "accumulating and writing once would lose everything on interruption"


async def test_add_ons_are_not_written_into_the_games_catalog():
    client = FakeStoreClient([page([product("P1", cls="Full Game"), product("P2", cls="Add-On")], is_last=True)])
    repository = FakeCatalogRepository()

    await _service(client, repository).backfill_category("cat-1")

    assert [p.product_id for p in repository.written[0]] == ["P1"]


async def test_a_page_of_only_add_ons_writes_nothing_but_keeps_walking():
    client = FakeStoreClient(
        [page([product("P1", cls="Add-On")], offset=0), page([product("P2")], offset=1, is_last=True)]
    )
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1")

    assert len(repository.written) == 1
    assert progress.pages_read == 2


async def test_resumes_from_the_reported_next_offset():
    client = FakeStoreClient([page([product("P1"), product("P2")], offset=40)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1", start_offset=40, max_pages=1)

    assert progress.completed is False
    assert progress.next_offset == 42, "offset must advance by products actually returned, not by page size"


async def test_a_short_page_does_not_skip_products():
    client = FakeStoreClient([page([product("P1")], offset=0)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1", max_pages=1)

    assert progress.next_offset == 1, "advancing by PAGE_SIZE here would skip 99 unseen products"


async def test_a_page_budget_stops_the_walk_and_says_so():
    client = FakeStoreClient([page([product(f"P{i}")], offset=i) for i in range(10)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1", max_pages=3)

    assert progress.pages_read == 3
    assert progress.completed is False
    assert progress.stopped_reason == "page_budget_exhausted"


async def test_a_rotated_query_hash_halts_the_walk_with_its_own_reason():
    client = FakeStoreClient([], error=StoreQueryRotatedError("hash not whitelisted"))
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1")

    assert progress.completed is False
    assert progress.stopped_reason == "query_rotated"
    assert repository.written == []


async def test_a_rotated_hash_stops_the_whole_run_not_just_one_category():
    client = FakeStoreClient([], error=StoreQueryRotatedError("hash not whitelisted"))
    repository = FakeCatalogRepository()

    summary = await _service(client, repository).backfill(["cat-1", "cat-2", "cat-3"])

    assert len(summary.categories) == 1, "the next category would fail identically; retrying it is noise"
    assert summary.completed is False


async def test_summary_totals_across_categories():
    client = FakeStoreClient(
        [
            page([product("P1"), product("P2")], is_last=True),
            page([product("P3")], is_last=True),
        ]
    )
    repository = FakeCatalogRepository()

    summary = await _service(client, repository).backfill(["cat-1", "cat-2"])

    assert summary.completed is True
    assert summary.games_created == 3
    assert summary.covers_cached == 3


async def test_an_empty_category_completes_without_writing():
    client = FakeStoreClient([page([], is_last=False)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1")

    assert progress.completed is True, "an empty page ends the walk even if the gateway did not set isLast"
    assert repository.written == []
