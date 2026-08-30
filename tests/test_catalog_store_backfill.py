"""Tests for the PlayStation Store catalog backfill walk, using hand-written fakes."""

from __future__ import annotations

from curator.catalog.store_backfill_service import StoreBackfillService
from curator.psn.store_client import (
    FULL_GAME_FILTER,
    StoreCategoryPage,
    StoreFilterIgnoredError,
    StoreProduct,
    StoreQueryRotatedError,
)


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
        self.filters: list[tuple[str, ...]] = []

    async def category_page(self, category_id, *, offset=0, size=100, filter_by=()):
        self.calls.append((category_id, offset, size))
        self.filters.append(tuple(filter_by))
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


async def test_backfill_resumes_each_category_from_its_own_start_offset():
    client = FakeStoreClient([page([product("p1")], offset=1200, is_last=True)])
    service = _service(client, FakeCatalogRepository())

    await service.backfill(["cat-1"], start_offsets={"cat-1": 1200})

    assert client.calls[0] == ("cat-1", 1200, 100)


async def test_backfill_starts_a_category_at_zero_when_it_has_no_start_offset():
    client = FakeStoreClient([page([product("p1")], is_last=True), page([product("p2")], offset=500, is_last=True)])
    service = _service(client, FakeCatalogRepository())

    await service.backfill(["cat-1", "cat-2"], start_offsets={"cat-2": 500})

    assert [(category, offset) for category, offset, _ in client.calls] == [("cat-1", 0), ("cat-2", 500)]


async def test_a_resumed_walk_is_not_blamed_for_products_an_earlier_run_already_read():
    client = FakeStoreClient([page([product("p1")], offset=900, is_last=True, total=1000)])
    service = _service(client, FakeCatalogRepository())

    summary = await service.backfill(["cat-1"], start_offsets={"cat-1": 900})

    assert summary.categories[0].coverage_shortfall == 99


async def test_asks_the_gateway_for_full_games_only():
    client = FakeStoreClient([page([product("P1")], offset=0, is_last=True)])

    await _service(client, FakeCatalogRepository()).backfill_category("cat-1")

    assert client.filters == [(FULL_GAME_FILTER,)]


async def test_an_unhonoured_filter_stops_the_walk_instead_of_seeding_a_wrong_catalog():
    client = FakeStoreClient([], error=StoreFilterIgnoredError("ignored"))
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1")

    assert not progress.completed
    assert progress.stopped_reason == "filter_not_applied"
    assert repository.written == []


async def test_an_unhonoured_filter_stops_every_remaining_category_too():
    client = FakeStoreClient([], error=StoreFilterIgnoredError("ignored"))

    summary = await _service(client, FakeCatalogRepository()).backfill(["cat-1", "cat-2", "cat-3"])

    assert [p.category_id for p in summary.categories] == ["cat-1"]


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


async def test_a_resumed_walk_is_only_accountable_for_the_span_beyond_its_start_offset():
    client = FakeStoreClient([page([product("P7"), product("P8")], offset=6, total=8, is_last=True)])

    progress = await _service(client, FakeCatalogRepository()).backfill_category("cat-1", start_offset=6)

    assert progress.completed
    assert progress.distinct_products == 2
    assert progress.reported_total == 8
    assert progress.coverage_shortfall == 0


async def test_a_resumed_walk_still_reports_a_real_gap_within_its_own_span():
    client = FakeStoreClient([page([product("P7")], offset=6, total=9, is_last=True)])

    progress = await _service(client, FakeCatalogRepository()).backfill_category("cat-1", start_offset=6)

    assert progress.coverage_shortfall == 2


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


async def test_a_category_yielding_no_products_at_all_does_not_report_success():
    client = FakeStoreClient([page([], is_last=False)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1")

    assert progress.stopped_reason == "no_products", (
        "a mistyped category id answers 200 with an empty grid, and reporting that as a completed walk "
        "is indistinguishable from having backfilled the whole category"
    )
    assert progress.completed is False
    assert repository.written == []


async def test_resuming_past_the_end_of_a_category_still_completes():
    client = FakeStoreClient([page([], is_last=True)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category("cat-1", start_offset=500)

    assert progress.completed is True, (
        "an empty page from a non-zero offset is the end of a walk that already read products, not a "
        "category that yielded nothing"
    )
    assert progress.stopped_reason is None
    assert repository.written == []
