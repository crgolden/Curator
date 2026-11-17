"""Tests for the PlayStation Store catalog backfill walk, using hand-written fakes."""

from __future__ import annotations

from curator.catalog.store_backfill_service import (
    FILTER_NOT_APPLIED,
    NO_PRODUCTS,
    PAGE_BUDGET_EXHAUSTED,
    PAGE_SIZE,
    QUERY_ROTATED,
    StoreBackfillService,
)
from curator.psn.store_client import (
    FULL_GAME_CLASSIFICATION,
    FULL_GAME_FILTER,
    StoreCategoryPage,
    StoreFilterIgnoredError,
    StoreProduct,
    StoreQueryRotatedError,
)
from curator.psn.title_platform import PS4
from test_values import (
    lowercase_token,
    new_category_id,
    new_cover_image_url,
    new_game_title,
    new_positive_count,
    new_ps4_title_id,
    new_small_count,
    new_store_product_id,
)

NOT_A_FULL_GAME = new_game_title()
_ANY_TOTAL = new_positive_count()


def product(product_id, cls=FULL_GAME_CLASSIFICATION):
    return StoreProduct(
        product_id=product_id,
        name=new_game_title(),
        platforms=(PS4,),
        np_title_id=new_ps4_title_id(),
        cover_image_url=new_cover_image_url(),
        classification=cls,
    )


def any_product():
    return product(new_store_product_id())


class FakeStoreClient:
    def __init__(self, pages, error=None):
        self._pages = list(pages)
        self._error = error
        self.calls: list[tuple[str, int, int]] = []
        self.filters: list[tuple[str, ...]] = []

    async def category_page(self, category_id, *, offset=0, size=PAGE_SIZE, filter_by=()):
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


def page(products, *, offset=0, is_last=False, total=_ANY_TOTAL):
    return StoreCategoryPage(products=tuple(products), total_count=total, offset=offset, is_last=is_last)


def _service(client, repository):
    return StoreBackfillService(client, repository, page_delay_seconds=0)


async def test_backfill_resumes_each_category_from_its_own_start_offset():
    category_id = new_category_id()
    start_offset = new_positive_count()
    client = FakeStoreClient([page([any_product()], offset=start_offset, is_last=True)])
    service = _service(client, FakeCatalogRepository())

    await service.backfill([category_id], start_offsets={category_id: start_offset})

    assert client.calls[0] == (category_id, start_offset, PAGE_SIZE)


async def test_backfill_starts_a_category_at_zero_when_it_has_no_start_offset():
    first_category, second_category = new_category_id(), new_category_id()
    second_offset = new_positive_count()
    client = FakeStoreClient(
        [page([any_product()], is_last=True), page([any_product()], offset=second_offset, is_last=True)]
    )
    service = _service(client, FakeCatalogRepository())

    await service.backfill([first_category, second_category], start_offsets={second_category: second_offset})

    assert [(category, offset) for category, offset, _ in client.calls] == [
        (first_category, 0),
        (second_category, second_offset),
    ]


async def test_a_resumed_walk_is_not_blamed_for_products_an_earlier_run_already_read():
    category_id = new_category_id()
    start_offset = new_positive_count()
    unread_span = new_positive_count() + 1
    client = FakeStoreClient(
        [page([any_product()], offset=start_offset, is_last=True, total=start_offset + unread_span)]
    )
    service = _service(client, FakeCatalogRepository())

    summary = await service.backfill([category_id], start_offsets={category_id: start_offset})

    assert summary.categories[0].coverage_shortfall == unread_span - 1


async def test_asks_the_gateway_for_full_games_only():
    client = FakeStoreClient([page([any_product()], offset=0, is_last=True)])

    await _service(client, FakeCatalogRepository()).backfill_category(new_category_id())

    assert client.filters == [(FULL_GAME_FILTER,)]


async def test_an_unhonoured_filter_stops_the_walk_instead_of_seeding_a_wrong_catalog():
    client = FakeStoreClient([], error=StoreFilterIgnoredError(lowercase_token()))
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category(new_category_id())

    assert not progress.completed
    assert progress.stopped_reason == FILTER_NOT_APPLIED
    assert repository.written == []


async def test_an_unhonoured_filter_stops_every_remaining_category_too():
    first_category = new_category_id()
    client = FakeStoreClient([], error=StoreFilterIgnoredError(lowercase_token()))

    summary = await _service(client, FakeCatalogRepository()).backfill(
        [first_category, new_category_id(), new_category_id()]
    )

    assert [p.category_id for p in summary.categories] == [first_category]


async def test_a_delisting_mid_walk_is_measured_against_the_final_reported_total():
    first_page_products = [any_product(), any_product()]
    second_page_products = [any_product()]
    final_total = len(first_page_products) + len(second_page_products)
    client = FakeStoreClient(
        [
            page(first_page_products, offset=0, total=final_total + 1),
            page(second_page_products, offset=len(first_page_products), total=final_total, is_last=True),
        ]
    )
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category(new_category_id())

    assert progress.completed
    assert progress.distinct_products == final_total
    assert progress.reported_total == final_total
    assert progress.coverage_shortfall == 0, "the final total is what the walk is measured against"


async def test_a_resumed_walk_is_only_accountable_for_the_span_beyond_its_start_offset():
    start_offset = new_positive_count()
    products = [any_product(), any_product()]
    total = start_offset + len(products)
    client = FakeStoreClient([page(products, offset=start_offset, total=total, is_last=True)])

    progress = await _service(client, FakeCatalogRepository()).backfill_category(
        new_category_id(), start_offset=start_offset
    )

    assert progress.completed
    assert progress.distinct_products == len(products)
    assert progress.reported_total == total
    assert progress.coverage_shortfall == 0


async def test_a_resumed_walk_still_reports_a_real_gap_within_its_own_span():
    start_offset = new_positive_count()
    gap = new_small_count()
    products = [any_product()]
    client = FakeStoreClient(
        [page(products, offset=start_offset, total=start_offset + len(products) + gap, is_last=True)]
    )

    progress = await _service(client, FakeCatalogRepository()).backfill_category(
        new_category_id(), start_offset=start_offset
    )

    assert progress.coverage_shortfall == gap


async def test_a_short_walk_against_a_larger_total_reports_the_gap():
    gap = new_small_count()
    products = [any_product(), any_product()]
    client = FakeStoreClient([page(products, offset=0, total=len(products) + gap, is_last=True)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category(new_category_id())

    assert progress.completed
    assert progress.distinct_products == len(products)
    assert progress.coverage_shortfall == gap


async def test_repeated_products_across_pages_are_not_double_counted_as_coverage():
    first_id, repeated_id, last_id = new_store_product_id(), new_store_product_id(), new_store_product_id()
    distinct_total = len((first_id, repeated_id, last_id))
    first_page_products = [product(first_id), product(repeated_id)]
    second_page_products = [product(repeated_id), product(last_id)]
    client = FakeStoreClient(
        [
            page(first_page_products, offset=0, total=distinct_total),
            page(second_page_products, offset=len(first_page_products), total=distinct_total, is_last=True),
        ]
    )
    repository = FakeCatalogRepository()
    fetched_count = len(first_page_products) + len(second_page_products)

    progress = await _service(client, repository).backfill_category(new_category_id())

    assert progress.products_seen == fetched_count, "raw count still reflects what was fetched"
    assert progress.distinct_products == distinct_total
    assert progress.coverage_shortfall == 0


async def test_a_walk_stopped_by_its_page_budget_reports_no_shortfall():
    client = FakeStoreClient([page([any_product()], offset=0)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category(new_category_id(), max_pages=1)

    assert not progress.completed
    assert progress.stopped_reason == PAGE_BUDGET_EXHAUSTED
    assert progress.coverage_shortfall == 0


async def test_walks_until_the_gateway_says_it_is_the_last_page():
    pages = [
        page([any_product()], offset=0),
        page([any_product()], offset=1),
        page([any_product()], is_last=True),
    ]
    client = FakeStoreClient(pages)
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category(new_category_id())

    assert progress.completed is True
    assert progress.pages_read == len(pages)
    assert progress.products_seen == len(pages)


async def test_terminates_on_is_last_rather_than_on_a_drifting_total_count():
    client = FakeStoreClient([page([any_product()], offset=0, is_last=True, total=new_positive_count())])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category(new_category_id())

    assert progress.completed is True, "a totalCount that moves between calls must not drive termination"
    assert progress.pages_read == 1


async def test_writes_each_page_as_it_is_read_so_an_interrupted_walk_still_seeds():
    pages = [page([any_product()], offset=0), page([any_product()], offset=1, is_last=True)]
    client = FakeStoreClient(pages)
    repository = FakeCatalogRepository()

    await _service(client, repository).backfill_category(new_category_id())

    written, page_count = repository.written, len(pages)
    assert len(written) == page_count, "accumulating and writing once would lose everything on interruption"


async def test_add_ons_are_not_written_into_the_games_catalog():
    full_game_id = new_store_product_id()
    add_on = product(new_store_product_id(), cls=NOT_A_FULL_GAME)
    client = FakeStoreClient([page([product(full_game_id, cls=FULL_GAME_CLASSIFICATION), add_on], is_last=True)])
    repository = FakeCatalogRepository()

    await _service(client, repository).backfill_category(new_category_id())

    assert [p.product_id for p in repository.written[0]] == [full_game_id]


async def test_a_page_of_only_add_ons_writes_nothing_but_keeps_walking():
    pages = [
        page([product(new_store_product_id(), cls=NOT_A_FULL_GAME)], offset=0),
        page([any_product()], offset=1, is_last=True),
    ]
    client = FakeStoreClient(pages)
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category(new_category_id())

    assert len(repository.written) == 1
    assert progress.pages_read == len(pages)


async def test_resumes_from_the_reported_next_offset():
    start_offset = new_positive_count()
    products = [any_product(), any_product()]
    client = FakeStoreClient([page(products, offset=start_offset)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category(
        new_category_id(), start_offset=start_offset, max_pages=1
    )

    assert progress.completed is False
    next_offset, expected_offset = progress.next_offset, start_offset + len(products)
    assert next_offset == expected_offset, "offset must advance by products actually returned, not by page size"


async def test_a_short_page_does_not_skip_products():
    client = FakeStoreClient([page([any_product()], offset=0)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category(new_category_id(), max_pages=1)

    assert progress.next_offset == 1, "advancing by PAGE_SIZE here would skip 99 unseen products"


async def test_a_page_budget_stops_the_walk_and_says_so():
    page_budget = new_small_count()
    page_count = page_budget + new_small_count()
    client = FakeStoreClient([page([any_product()], offset=index) for index in range(page_count)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category(new_category_id(), max_pages=page_budget)

    assert progress.pages_read == page_budget
    assert progress.completed is False
    assert progress.stopped_reason == PAGE_BUDGET_EXHAUSTED


async def test_a_rotated_query_hash_halts_the_walk_with_its_own_reason():
    client = FakeStoreClient([], error=StoreQueryRotatedError(lowercase_token()))
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category(new_category_id())

    assert progress.completed is False
    assert progress.stopped_reason == QUERY_ROTATED
    assert repository.written == []


async def test_a_rotated_hash_stops_the_whole_run_not_just_one_category():
    client = FakeStoreClient([], error=StoreQueryRotatedError(lowercase_token()))
    repository = FakeCatalogRepository()

    summary = await _service(client, repository).backfill([new_category_id(), new_category_id(), new_category_id()])

    assert len(summary.categories) == 1, "the next category would fail identically; retrying it is noise"
    assert summary.completed is False


async def test_summary_totals_across_categories():
    first_page_products = [any_product(), any_product()]
    second_page_products = [any_product()]
    client = FakeStoreClient(
        [
            page(first_page_products, is_last=True),
            page(second_page_products, is_last=True),
        ]
    )
    repository = FakeCatalogRepository()
    product_count = len(first_page_products) + len(second_page_products)

    summary = await _service(client, repository).backfill([new_category_id(), new_category_id()])

    assert summary.completed is True
    assert summary.games_created == product_count
    assert summary.covers_cached == product_count


async def test_a_category_yielding_no_products_at_all_does_not_report_success():
    client = FakeStoreClient([page([], is_last=False)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category(new_category_id())

    assert progress.stopped_reason == NO_PRODUCTS, (
        "a mistyped category id answers 200 with an empty grid, and reporting that as a completed walk "
        "is indistinguishable from having backfilled the whole category"
    )
    assert progress.completed is False
    assert repository.written == []


async def test_resuming_past_the_end_of_a_category_still_completes():
    client = FakeStoreClient([page([], is_last=True)])
    repository = FakeCatalogRepository()

    progress = await _service(client, repository).backfill_category(
        new_category_id(), start_offset=new_positive_count()
    )

    assert progress.completed is True, (
        "an empty page from a non-zero offset is the end of a walk that already read products, not a "
        "category that yielded nothing"
    )
    assert progress.stopped_reason is None
    assert repository.written == []
