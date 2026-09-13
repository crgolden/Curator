"""Tests for the PS Plus catalog walk, using a fake storefront client and a fake membership writer."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from curator.catalog.ps_plus_repository import PsPlusCategory
from curator.catalog.ps_plus_walk_service import (
    CATEGORY_RENAMED,
    NO_PRODUCTS,
    PAGE_BUDGET_EXHAUSTED,
    QUERY_ROTATED,
    PsPlusWalkService,
)
from curator.psn.store_client import StoreCategoryPage, StoreProduct, StoreQueryRotatedError
from test_values import (
    new_category_id,
    new_game_title,
    new_ps4_title_id,
    new_reporting_name,
    new_store_product_id,
    new_walk_id,
)

_PREFIX = "SPAR_GMA_PSGC"


def category(prefix=_PREFIX, tier="extra"):
    return PsPlusCategory(category_id=new_category_id(), tier=tier, locale="en-US", reporting_name_prefix=prefix)


def product(title_id=None, *, raw=None):
    title_id = title_id or new_ps4_title_id()
    return StoreProduct(
        product_id=new_store_product_id(title_id),
        name=new_game_title(),
        platforms=("PS4",),
        np_title_id=title_id,
        cover_image_url=None,
        classification="Full Game",
        raw=raw if raw is not None else {"price": {"upsellText": None}},
    )


def page(products, *, offset=0, is_last=False, total=None, reporting_name=None, prefix=_PREFIX):
    return StoreCategoryPage(
        products=tuple(products),
        total_count=len(products) if total is None else total,
        offset=offset,
        is_last=is_last,
        reporting_name=reporting_name or new_reporting_name(prefix),
    )


class FakeStoreClient:
    def __init__(self, pages, *, raise_on_page=None):
        self._pages = list(pages)
        self._raise_on_page = raise_on_page
        self.calls: list[tuple[str, int, int]] = []

    async def category_page(self, category_id, *, offset=0, size=100, filter_by=()):
        self.calls.append((category_id, offset, size))
        if self._raise_on_page is not None and len(self.calls) == self._raise_on_page:
            raise StoreQueryRotatedError("rotated")
        return self._pages.pop(0)


class FakeWalkWriter:
    def __init__(self):
        self.walk_id = new_walk_id()
        self.recorded: list[list[StoreProduct]] = []
        self.completed: list[tuple[str, datetime, int, int]] = []
        self.departures: list[tuple[str, datetime]] = []
        self.stops: list[tuple[str, str, int | None, int]] = []

    async def begin(self, started_at):
        return self.walk_id

    async def record_products(self, walk_id, products, seen_at):
        self.recorded.append(list(products))
        return len(products)

    async def complete(self, walk_id, completed_at, reported_total, distinct_products):
        self.completed.append((walk_id, completed_at, reported_total, distinct_products))

    async def mark_departures(self, walk_id, completed_at):
        self.departures.append((walk_id, completed_at))
        return 1

    async def stop(self, walk_id, reason, reported_total, distinct_products):
        self.stops.append((walk_id, reason, reported_total, distinct_products))


class FakePsPlusRepository:
    def __init__(self, categories):
        self._categories = list(categories)
        self.writers: list[FakeWalkWriter] = []
        self.locked: list[str] = []

    async def list_categories(self):
        return list(self._categories)

    @asynccontextmanager
    async def walk(self, category_id):
        self.locked.append(category_id)
        writer = FakeWalkWriter()
        self.writers.append(writer)
        yield writer


class FixedClock:
    def __init__(self):
        self.now = datetime(2026, 9, 1, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


def _service(client, repository):
    return PsPlusWalkService(client, repository, page_delay_seconds=0, clock=FixedClock())


async def test_a_walk_interrupted_on_page_two_marks_no_departure():
    walked = category()
    client = FakeStoreClient([page([product()], total=2)], raise_on_page=2)
    repository = FakePsPlusRepository([walked])

    progress = await _service(client, repository).walk_category(walked)

    writer = repository.writers[0]
    assert not progress.completed
    assert progress.stopped_reason == QUERY_ROTATED
    assert writer.departures == [], "a partial walk can never say a title left the catalog"
    assert writer.completed == []
    assert writer.stops[0][1] == QUERY_ROTATED


async def test_a_completed_walk_marks_departures_only_when_nothing_was_missed():
    walked = category()
    client = FakeStoreClient([page([product(), product()], is_last=True)])
    repository = FakePsPlusRepository([walked])

    progress = await _service(client, repository).walk_category(walked)

    writer = repository.writers[0]
    assert progress.completed
    assert progress.coverage_shortfall == 0
    assert [walk_id for walk_id, _ in writer.departures] == [writer.walk_id]


async def test_a_completed_walk_with_a_coverage_shortfall_marks_no_departure():
    walked = category()
    client = FakeStoreClient([page([product()], is_last=True, total=5)])
    repository = FakePsPlusRepository([walked])

    progress = await _service(client, repository).walk_category(walked)

    assert progress.completed
    assert progress.coverage_shortfall == 4
    assert repository.writers[0].departures == []


async def test_a_category_whose_reporting_name_lost_its_prefix_stops_and_writes_nothing():
    walked = category(prefix=_PREFIX)
    client = FakeStoreClient([page([product()], is_last=True, reporting_name="WM_EU_ALL_PS4_GAMES")])
    repository = FakePsPlusRepository([walked])

    progress = await _service(client, repository).walk_category(walked)

    writer = repository.writers[0]
    assert progress.stopped_reason == CATEGORY_RENAMED
    assert writer.recorded == [], "a renamed category is the wrong thing to walk, so nothing is recorded"
    assert writer.departures == []


async def test_a_product_with_no_upsell_text_is_still_recorded():
    """Membership is category membership; the walk never reads the display string."""
    walked = category()
    member = product(raw={"price": {"upsellText": None}})
    client = FakeStoreClient([page([member], is_last=True)])
    repository = FakePsPlusRepository([walked])

    await _service(client, repository).walk_category(walked)

    assert repository.writers[0].recorded == [[member]]


async def test_a_bundle_is_a_member_too():
    walked = category()
    bundle = StoreProduct(
        product_id=new_store_product_id(),
        name=new_game_title(),
        platforms=("PS5",),
        np_title_id=new_ps4_title_id(),
        cover_image_url=None,
        classification="Game Bundle",
    )
    client = FakeStoreClient([page([bundle], is_last=True)])
    repository = FakePsPlusRepository([walked])

    await _service(client, repository).walk_category(walked)

    assert repository.writers[0].recorded == [[bundle]]


async def test_the_walk_asks_for_no_facet_filter():
    walked = category()
    client = FakeStoreClient([page([product()], is_last=True)])

    await _service(client, FakePsPlusRepository([walked])).walk_category(walked)

    assert client.calls == [(walked.category_id, 0, 100)]


async def test_an_empty_first_page_stops_as_no_products():
    walked = category()
    client = FakeStoreClient([page([], is_last=True, total=0)])
    repository = FakePsPlusRepository([walked])

    progress = await _service(client, repository).walk_category(walked)

    assert progress.stopped_reason == NO_PRODUCTS
    assert not progress.completed
    assert repository.writers[0].departures == []


async def test_a_page_budget_stops_short_and_marks_no_departure():
    walked = category()
    client = FakeStoreClient([page([product()], total=3), page([product()], offset=1, total=3)])
    repository = FakePsPlusRepository([walked])

    progress = await _service(client, repository).walk_category(walked, max_pages=1)

    assert progress.stopped_reason == PAGE_BUDGET_EXHAUSTED
    assert progress.pages_read == 1
    assert repository.writers[0].departures == []


async def test_walk_all_takes_each_category_under_its_own_lock():
    extra, premium = category(tier="extra"), category(tier="premium", prefix="SPAR_GMA_PSPLUS_CC")
    client = FakeStoreClient(
        [page([product()], is_last=True), page([product()], is_last=True, prefix="SPAR_GMA_PSPLUS_CC")]
    )
    repository = FakePsPlusRepository([extra, premium])

    results = await _service(client, repository).walk_all()

    assert [progress.tier for progress in results] == ["extra", "premium"]
    assert repository.locked == [extra.category_id, premium.category_id]


async def test_walk_all_stops_at_the_first_rotated_query():
    extra, premium = category(tier="extra"), category(tier="premium", prefix="SPAR_GMA_PSPLUS_CC")
    client = FakeStoreClient([], raise_on_page=1)
    repository = FakePsPlusRepository([extra, premium])

    results = await _service(client, repository).walk_all()

    assert [progress.category_id for progress in results] == [extra.category_id]
