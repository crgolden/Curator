"""Tests for the anonymous PlayStation Store catalog client, using an httpx MockTransport."""

from __future__ import annotations

import json
import random
from urllib.parse import urlparse

import httpx
import pytest

from curator.http_headers import AUTHORIZATION_HEADER, COOKIE_HEADER
from curator.psn._graphql import (
    DATA_KEY,
    ERRORS_KEY,
    EXTENSIONS_PARAM,
    GRAPHQL_URL,
    MESSAGE_KEY,
    PERSISTED_QUERY_KEY,
    SHA256_HASH_KEY,
    VARIABLES_PARAM,
)
from curator.psn._media import (
    GAMEHUB_COVER_ART_ROLE,
    IMAGE_MEDIA_TYPE,
    PORTRAIT_BANNER_ROLE,
    ROLE_KEY,
    TYPE_KEY,
    URL_KEY,
)
from curator.psn._product_node import (
    BASE_PRICE_KEY,
    CLASSIFICATION_KEY,
    DISCOUNT_TEXT_KEY,
    DISCOUNTED_PRICE_KEY,
    ID_KEY,
    IS_FREE_KEY,
    IS_TIED_TO_SUBSCRIPTION_KEY,
    MEDIA_KEY,
    NAME_KEY,
    NP_TITLE_ID_KEY,
    PLATFORMS_KEY,
    PRICE_KEY,
    TYPENAME_KEY,
)
from curator.psn.store_client import (
    APOLLO_OPERATION_NAME_HEADER,
    APOLLO_REQUIRE_PREFLIGHT_HEADER,
    APOLLO_REQUIRE_PREFLIGHT_VALUE,
    CATEGORY_GRID_RETRIEVE_OPERATION,
    CLASSIFICATION_FACET,
    FACET_NAME_KEY,
    FACET_OPTIONS_KEY,
    FACET_VALUE_COUNT_KEY,
    FACET_VALUE_KEY_KEY,
    FACET_VALUES_KEY,
    FILTER_BY_VARIABLE,
    FULL_GAME_CLASSIFICATION,
    FULL_GAME_FACET_KEY,
    FULL_GAME_FILTER,
    IS_ASCENDING_KEY,
    IS_LAST_KEY,
    NOT_WHITELISTED_MARKER,
    OFFSET_KEY,
    PAGE_ARGS_VARIABLE,
    PAGE_INFO_KEY,
    PRODUCT_RELEASE_DATE_SORT_FIELD,
    PRODUCTS_KEY,
    REPORTING_NAME_KEY,
    SIZE_KEY,
    SORT_BY_VARIABLE,
    SORT_FIELD_KEY,
    STORE_GRAPHQL_URL,
    TOTAL_COUNT_KEY,
    StoreCatalogClient,
    StoreCatalogError,
    StoreFilterIgnoredError,
    StoreQueryRotatedError,
    parse_price_cents,
)
from curator.psn.title_platform import PS4, PS5
from test_values import (
    lowercase_token,
    new_category_id,
    new_cover_image_url,
    new_facet_key,
    new_game_title,
    new_opaque_token,
    new_positive_count,
    new_price_cents,
    new_ps4_title_id,
    new_ps5_title_id,
    new_reporting_name,
    new_sha256_hash,
    new_store_product_id,
)


def _facet_values(values):
    return [{FACET_VALUE_KEY_KEY: key, FACET_VALUE_COUNT_KEY: count} for key, count in values.items()]


def _census(facets):
    return [{FACET_NAME_KEY: name, FACET_VALUES_KEY: _facet_values(values)} for name, values in facets.items()]


def _grid(products, total, *, offset=0, is_last=False, facets=None):
    grid = {
        PRODUCTS_KEY: products,
        PAGE_INFO_KEY: {TOTAL_COUNT_KEY: total, OFFSET_KEY: offset, SIZE_KEY: len(products), IS_LAST_KEY: is_last},
    }
    if facets is not None:
        grid[FACET_OPTIONS_KEY] = _census(facets)
    return {DATA_KEY: {CATEGORY_GRID_RETRIEVE_OPERATION: grid}}


def _classification_facets(full_games):
    """A classification census publishing ``full_games`` full games beside one other classification."""
    return {CLASSIFICATION_FACET: {FULL_GAME_FACET_KEY: full_games, new_facet_key(): new_positive_count()}}


def _media_entry(role, url, media_type=IMAGE_MEDIA_TYPE):
    return {ROLE_KEY: role, TYPE_KEY: media_type, URL_KEY: url}


def _other_role():
    return lowercase_token().upper()


def _video_type():
    return lowercase_token().upper()


def _product(*, product_id=None, name=None, platforms=(PS4,), np_title_id=None, media=None, cls=None):
    """A product in the shape the live gateway actually returns."""
    return {
        TYPENAME_KEY: lowercase_token(),
        ID_KEY: product_id or new_store_product_id(),
        NAME_KEY: name if name is not None else new_game_title(),
        PLATFORMS_KEY: list(platforms),
        NP_TITLE_ID_KEY: np_title_id or new_ps4_title_id(),
        CLASSIFICATION_KEY: cls or FULL_GAME_CLASSIFICATION,
        MEDIA_KEY: media if media is not None else [_media_entry(GAMEHUB_COVER_ART_ROLE, new_cover_image_url())],
    }


def _client(handler, **options):
    return StoreCatalogClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), **options)


def _answering(body, status_code=200):
    def handler(request):
        return httpx.Response(status_code, json=body)

    return handler


class Recorder:
    """Answers every request with ``body`` and keeps the last request it saw."""

    def __init__(self, body):
        self._body = body
        self.request: httpx.Request | None = None

    def __call__(self, request):
        self.request = request
        return httpx.Response(200, json=self._body)

    @property
    def variables(self):
        assert self.request is not None
        return json.loads(self.request.url.params[VARIABLES_PARAM])


def _hash_of(request):
    return json.loads(request.url.params[EXTENSIONS_PARAM])[PERSISTED_QUERY_KEY][SHA256_HASH_KEY]


def _rotated_body():
    return {MESSAGE_KEY: f"{lowercase_token()} {NOT_WHITELISTED_MARKER}"}


async def test_reads_a_page_and_the_category_total():
    total = new_positive_count()
    first = _product(platforms=(PS4,), np_title_id=new_ps4_title_id())
    second = _product(platforms=(PS5,), np_title_id=new_ps5_title_id())

    page = await _client(_answering(_grid([first, second], total))).category_page(new_category_id())

    assert page.total_count == total
    assert [p.product_id for p in page.products] == [first[ID_KEY], second[ID_KEY]]
    assert page.products[0].platforms == (PS4,)


async def test_carries_the_np_title_id_that_joins_onto_existing_curator_rows():
    np_title_id = new_ps4_title_id()

    page = await _client(_answering(_grid([_product(np_title_id=np_title_id)], 1))).category_page(new_category_id())

    assert page.products[0].np_title_id == np_title_id, (
        "npTitleId is what makes a backfilled product joinable to library_entries/psn_catalog_cache"
    )


async def test_the_whole_product_node_survives_the_projection():
    """price, sortingOptions, skus and the sibling concepts collection arrive inside a response the walk
    already pays for. Projecting six fields and dropping the rest means a second full walk to get any of
    them back, so the node is carried through and persisted by 0047."""
    unprojected_key, unprojected_value = lowercase_token(), [{ID_KEY: new_opaque_token()}]
    node = _product()
    node[unprojected_key] = unprojected_value

    page = await _client(_answering(_grid([node], 1))).category_page(new_category_id())

    assert page.products[0].raw[unprojected_key] == unprojected_value


async def test_resolves_cover_art_by_role_preference_not_array_order():
    cover_url = new_cover_image_url()
    media = [
        _media_entry(_other_role(), new_cover_image_url()),
        _media_entry(_other_role(), new_cover_image_url(), _video_type()),
        _media_entry(GAMEHUB_COVER_ART_ROLE, cover_url),
    ]

    page = await _client(_answering(_grid([_product(media=media)], 1))).category_page(new_category_id())

    assert page.products[0].cover_image_url == cover_url, "an unranked role listed first must not win"


async def test_falls_back_through_the_role_preference_and_ignores_video():
    portrait_url = new_cover_image_url()
    media = [
        _media_entry(GAMEHUB_COVER_ART_ROLE, new_cover_image_url(), _video_type()),
        _media_entry(PORTRAIT_BANNER_ROLE, portrait_url),
    ]

    page = await _client(_answering(_grid([_product(media=media)], 1))).category_page(new_category_id())

    assert page.products[0].cover_image_url == portrait_url


async def test_an_image_entry_with_no_url_is_skipped_rather_than_stringified_to_the_word_none():
    """An entry PSN sent without a ``url`` must not become the literal four-character cover URL ``"None"``
    -- that would be stored, served and rendered as a broken image with nothing failing."""
    media = [
        {ROLE_KEY: GAMEHUB_COVER_ART_ROLE, TYPE_KEY: IMAGE_MEDIA_TYPE},
        _media_entry(PORTRAIT_BANNER_ROLE, None),
    ]

    page = await _client(_answering(_grid([_product(media=media)], 1))).category_page(new_category_id())

    assert page.products[0].cover_image_url is None


async def test_an_image_entry_with_no_role_is_skipped_rather_than_stringified_to_the_word_none():
    """Same defect on the other key: a role-less entry keyed as ``"None"`` never matches the preference
    list, so it is silently dead weight rather than a cover."""
    portrait_url = new_cover_image_url()
    media = [
        {TYPE_KEY: IMAGE_MEDIA_TYPE, URL_KEY: new_cover_image_url()},
        _media_entry(PORTRAIT_BANNER_ROLE, portrait_url),
    ]

    page = await _client(_answering(_grid([_product(media=media)], 1))).category_page(new_category_id())

    assert page.products[0].cover_image_url == portrait_url


async def test_a_role_published_twice_resolves_to_the_last_entry_the_gateway_listed():
    """PSN can repeat a role. Last-wins is the policy both consolidated originals had; it is pinned here
    so a future rewrite to a first-match loop is a visible change rather than a silent one."""
    last_url = new_cover_image_url()
    media = [
        _media_entry(GAMEHUB_COVER_ART_ROLE, new_cover_image_url()),
        _media_entry(GAMEHUB_COVER_ART_ROLE, last_url),
    ]

    page = await _client(_answering(_grid([_product(media=media)], 1))).category_page(new_category_id())

    assert page.products[0].cover_image_url == last_url


async def test_a_product_the_gateway_left_unnamed_reports_no_name_rather_than_a_blank_one():
    """A blank title would be inserted as a game called "" -- ``None`` is what lets the repository skip it."""
    page = await _client(_answering(_grid([_product(name="   ")], 1))).category_page(new_category_id())

    assert page.products[0].name is None


async def test_a_product_with_no_usable_image_reports_none_rather_than_a_video_url():
    media = [_media_entry(GAMEHUB_COVER_ART_ROLE, new_cover_image_url(), _video_type())]

    page = await _client(_answering(_grid([_product(media=media)], 1))).category_page(new_category_id())

    assert page.products[0].cover_image_url is None


async def test_distinguishes_full_games_from_add_ons():
    products = [_product(cls=FULL_GAME_CLASSIFICATION), _product(cls=lowercase_token())]

    page = await _client(_answering(_grid(products, 2))).category_page(new_category_id())

    assert [product.is_full_game for product in page.products] == [True, False]


async def test_reports_is_last_so_a_walk_terminates_on_the_gateway_not_on_a_drifting_total():
    offset = new_positive_count()
    body = _grid([_product()], offset + 1, offset=offset, is_last=True)

    page = await _client(_answering(body)).category_page(new_category_id(), offset=offset)

    assert page.is_last is True
    assert page.offset == offset


async def test_calls_the_storefront_gateway_not_the_authenticated_mobile_one():
    recorder = Recorder(_grid([], 0))

    await _client(recorder).category_page(new_category_id())

    assert recorder.request is not None
    assert str(recorder.request.url).startswith(STORE_GRAPHQL_URL)
    assert recorder.request.url.host != urlparse(GRAPHQL_URL).hostname, (
        "the mobile gateway needs a PSN token and cannot enumerate; this client exists to avoid it"
    )


async def test_sends_no_credential_of_any_kind():
    recorder = Recorder(_grid([], 0))

    await _client(recorder).category_page(new_category_id())

    assert recorder.request is not None
    assert AUTHORIZATION_HEADER not in recorder.request.headers, "the storefront gateway is anonymous by design"
    assert COOKIE_HEADER not in recorder.request.headers


async def test_sends_the_apollo_preflight_header_the_gateway_demands():
    """Without the preflight header the gateway rejects the call as a possible CSRF, which is not an auth
    failure. The pin locks the value; the second assert proves the request carries it."""
    recorder = Recorder(_grid([], 0))

    await _client(recorder).category_page(new_category_id())

    assert recorder.request is not None
    assert APOLLO_REQUIRE_PREFLIGHT_VALUE == "true"
    assert recorder.request.headers[APOLLO_REQUIRE_PREFLIGHT_HEADER] == APOLLO_REQUIRE_PREFLIGHT_VALUE
    assert recorder.request.headers[APOLLO_OPERATION_NAME_HEADER] == CATEGORY_GRID_RETRIEVE_OPERATION


async def test_paging_arguments_reach_the_gateway():
    category_id, offset, size = new_category_id(), new_positive_count(), new_positive_count()
    recorder = Recorder(_grid([], 0))

    await _client(recorder).category_page(category_id, offset=offset, size=size)

    assert recorder.variables[ID_KEY] == category_id
    assert recorder.variables[PAGE_ARGS_VARIABLE] == {SIZE_KEY: size, OFFSET_KEY: offset}


async def test_a_rotated_persisted_query_hash_is_its_own_error():
    client = _client(_answering(_rotated_body(), 400))

    with pytest.raises(StoreQueryRotatedError) as excinfo:
        await client.category_page(new_category_id())

    assert "refresh it" in str(excinfo.value), "the message must point at the fix, not just report failure"


async def test_a_rotated_hash_falls_through_to_the_next_candidate():
    dead_hash, live_hash = new_sha256_hash(), new_sha256_hash()
    tried = []

    def handler(request):
        tried.append(_hash_of(request))
        if tried[-1] == dead_hash:
            return httpx.Response(400, json=_rotated_body())
        return httpx.Response(200, json=_grid([_product()], 1, is_last=True))

    page = await _client(handler, query_hashes=(dead_hash, live_hash)).category_page(new_category_id())

    assert tried == [dead_hash, live_hash], "candidates are tried in order, stopping at the first that works"
    assert len(page.products) == 1


async def test_the_rotated_error_only_surfaces_once_every_candidate_is_exhausted():
    client = _client(_answering(_rotated_body(), 400), query_hashes=(new_sha256_hash(), new_sha256_hash()))

    with pytest.raises(StoreQueryRotatedError):
        await client.category_page(new_category_id())


async def test_facet_census_returns_every_published_key_with_its_count():
    facet_name = new_facet_key()
    census = {new_facet_key(): new_positive_count(), new_facet_key(): new_positive_count()}
    body = _grid([_product()], new_positive_count(), facets={facet_name: census})

    assert await _client(_answering(body)).facet_census(new_category_id(), facet_name) == census


async def test_facet_census_is_none_when_the_category_publishes_no_such_facet():
    body = _grid([_product()], new_positive_count(), facets={new_facet_key(): {PS5: new_positive_count()}})

    assert await _client(_answering(body)).facet_census(new_category_id(), new_facet_key()) is None


async def test_facet_census_costs_one_product_page_because_the_census_spans_the_category():
    facet_name = new_facet_key()
    recorder = Recorder(_grid([_product()], new_positive_count(), facets={facet_name: {new_facet_key(): 1}}))

    await _client(recorder).facet_census(new_category_id(), facet_name)

    assert recorder.variables[PAGE_ARGS_VARIABLE] == {SIZE_KEY: 1, OFFSET_KEY: 0}
    assert recorder.variables[FILTER_BY_VARIABLE] == []


async def test_facet_census_shares_the_hash_rotation_rather_than_reimplementing_it():
    dead_hash, live_hash = new_sha256_hash(), new_sha256_hash()
    facet_name = new_facet_key()
    census = {new_facet_key(): new_positive_count()}
    tried = []

    def handler(request):
        tried.append(_hash_of(request))
        if tried[-1] == dead_hash:
            return httpx.Response(400, json=_rotated_body())
        return httpx.Response(200, json=_grid([_product()], 1, facets={facet_name: census}))

    result = await _client(handler, query_hashes=(dead_hash, live_hash)).facet_census(new_category_id(), facet_name)

    assert tried == [dead_hash, live_hash]
    assert result == census


async def test_walks_in_ascending_release_date_so_a_new_release_cannot_shift_the_walk():
    """The pin locks the sort field; the second assert proves the request carries it."""
    recorder = Recorder(_grid([], 0))

    await _client(recorder).category_page(new_category_id())

    assert PRODUCT_RELEASE_DATE_SORT_FIELD == "productReleaseDate"
    assert recorder.variables[SORT_BY_VARIABLE] == {
        SORT_FIELD_KEY: PRODUCT_RELEASE_DATE_SORT_FIELD,
        IS_ASCENDING_KEY: True,
    }


async def test_requested_filters_reach_the_gateway():
    full_games = new_positive_count()
    recorder = Recorder(_grid([], full_games, facets=_classification_facets(full_games)))

    await _client(recorder).category_page(new_category_id(), filter_by=(FULL_GAME_FILTER,))

    assert recorder.variables[FILTER_BY_VARIABLE] == [FULL_GAME_FILTER]


async def test_a_filter_that_narrowed_to_its_facet_count_is_accepted():
    full_games = new_positive_count()
    body = _grid([_product()], full_games, facets=_classification_facets(full_games))

    page = await _client(_answering(body)).category_page(new_category_id(), filter_by=(FULL_GAME_FILTER,))

    assert page.total_count == full_games


async def test_a_silently_ignored_filter_is_rejected_rather_than_trusted():
    full_games = new_positive_count()
    unfiltered_total = full_games + new_positive_count()
    body = _grid([_product()], unfiltered_total, facets=_classification_facets(full_games))
    client = _client(_answering(body))

    with pytest.raises(StoreFilterIgnoredError) as excinfo:
        await client.category_page(new_category_id(), filter_by=(FULL_GAME_FILTER,))

    message = str(excinfo.value)
    assert str(full_games) in message, "the message must name what the category says the facet holds"
    assert str(unfiltered_total) in message, "the message must name what the filtered query actually returned"


async def test_a_filter_matching_nothing_is_rejected_rather_than_ending_the_walk():
    unpublished_key = new_facet_key()
    body = _grid([], 0, is_last=True, facets=_classification_facets(new_positive_count()))
    client = _client(_answering(body))

    with pytest.raises(StoreFilterIgnoredError) as excinfo:
        await client.category_page(new_category_id(), filter_by=(f"{CLASSIFICATION_FACET}:{unpublished_key}",))

    assert unpublished_key in str(excinfo.value)


async def test_a_zero_result_on_a_published_key_is_rejected_too():
    body = _grid([], 0, is_last=True, facets=_classification_facets(new_positive_count()))
    client = _client(_answering(body))

    with pytest.raises(StoreFilterIgnoredError):
        await client.category_page(new_category_id(), filter_by=(FULL_GAME_FILTER,))


async def test_an_unfiltered_page_is_never_checked_against_facets():
    full_games = new_positive_count()
    unfiltered_total = full_games + new_positive_count()
    body = _grid([_product()], unfiltered_total, facets=_classification_facets(full_games))

    page = await _client(_answering(body)).category_page(new_category_id())

    assert page.total_count == unfiltered_total


async def test_a_category_publishing_no_census_for_the_facet_is_allowed_through():
    total = new_positive_count()
    body = _grid([_product()], total, facets={new_facet_key(): {PS5: total}})

    page = await _client(_answering(body)).category_page(new_category_id(), filter_by=(FULL_GAME_FILTER,))

    assert page.total_count == total


async def test_a_response_without_any_facets_cannot_disprove_the_filter_so_is_allowed():
    total = new_positive_count()

    page = await _client(_answering(_grid([_product()], total))).category_page(
        new_category_id(), filter_by=(FULL_GAME_FILTER,)
    )

    assert page.total_count == total


async def test_other_graphql_errors_are_not_reported_as_a_rotated_hash():
    client = _client(_answering({ERRORS_KEY: [{MESSAGE_KEY: lowercase_token()}]}))

    with pytest.raises(StoreCatalogError) as excinfo:
        await client.category_page(new_category_id())

    assert not isinstance(excinfo.value, StoreQueryRotatedError)


async def test_a_server_error_is_surfaced_without_parsing_the_body():
    def handler(request):
        return httpx.Response(503, text=lowercase_token())

    with pytest.raises(StoreCatalogError):
        await _client(handler).category_page(new_category_id())


async def test_a_csrf_rejection_is_not_mistaken_for_a_rotated_hash():
    client = _client(_answering({ERRORS_KEY: [{MESSAGE_KEY: new_game_title()}]}, 400))

    with pytest.raises(StoreCatalogError) as excinfo:
        await client.category_page(new_category_id())

    assert not isinstance(excinfo.value, StoreQueryRotatedError)


async def test_products_without_an_id_are_skipped_rather_than_crashing_the_walk():
    identified = _product()
    body = _grid([{NAME_KEY: new_game_title()}, identified], 2)

    page = await _client(_answering(body)).category_page(new_category_id())

    assert [p.product_id for p in page.products] == [identified[ID_KEY]]


async def test_an_empty_page_is_not_an_error():
    offset = new_positive_count()

    page = await _client(_answering(_grid([], 0, offset=offset, is_last=True))).category_page(
        new_category_id(), offset=offset
    )

    assert (page.products, page.total_count, page.offset, page.is_last) == ((), 0, offset, True)


async def test_offset_falls_back_to_the_request_when_the_gateway_omits_it():
    offset = new_positive_count()
    body: dict[str, object] = {DATA_KEY: {CATEGORY_GRID_RETRIEVE_OPERATION: {PRODUCTS_KEY: [], PAGE_INFO_KEY: {}}}}

    page = await _client(_answering(body)).category_page(new_category_id(), offset=offset)

    assert page.offset == offset


async def test_the_gateways_reporting_name_rides_on_the_page():
    reporting_name = new_reporting_name(lowercase_token().upper())
    body = _grid([_product()], 1)
    body[DATA_KEY][CATEGORY_GRID_RETRIEVE_OPERATION][REPORTING_NAME_KEY] = reporting_name

    page = await _client(_answering(body)).category_page(new_category_id())

    assert page.reporting_name == reporting_name


async def test_a_page_without_a_reporting_name_carries_none_rather_than_a_blank():
    page = await _client(_answering(_grid([_product()], 1))).category_page(new_category_id())

    assert page.reporting_name is None


def _display_price(cents):
    return f"${cents // 100:,}.{cents % 100:02d}"


async def test_the_price_node_is_parsed_into_cents():
    base_cents, discounted_cents, discount_text = new_price_cents(), new_price_cents(), lowercase_token()
    node = _product()
    node[PRICE_KEY] = {
        BASE_PRICE_KEY: _display_price(base_cents),
        DISCOUNTED_PRICE_KEY: _display_price(discounted_cents),
        DISCOUNT_TEXT_KEY: discount_text,
        IS_FREE_KEY: False,
        IS_TIED_TO_SUBSCRIPTION_KEY: True,
    }

    page = await _client(_answering(_grid([node], 1))).category_page(new_category_id())

    price = page.products[0].price
    assert price is not None
    assert (price.base_cents, price.discounted_cents, price.discount_text) == (
        base_cents,
        discounted_cents,
        discount_text,
    )
    assert (price.is_free, price.tied_to_subscription) == (False, True)


def test_a_display_price_with_a_thousands_separator_parses_to_cents():
    cents = random.randint(100_000, 99_999_999)

    assert parse_price_cents(_display_price(cents)) == cents


def _worded_price():
    return lowercase_token().capitalize()


def _absent_price():
    return None


@pytest.mark.parametrize("make_display_price", [_worded_price, _absent_price])
def test_display_prices_that_are_not_amounts_parse_to_none(make_display_price):
    assert parse_price_cents(make_display_price()) is None


async def test_a_product_without_a_price_node_has_no_price():
    page = await _client(_answering(_grid([_product()], 1))).category_page(new_category_id())

    assert page.products[0].price is None
