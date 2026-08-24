"""Tests for the anonymous PlayStation Store catalog client, using an httpx MockTransport."""

from __future__ import annotations

import json

import httpx
import pytest

from curator.psn.store_client import (
    FULL_GAME_FILTER,
    STORE_GRAPHQL_URL,
    StoreCatalogClient,
    StoreCatalogError,
    StoreFilterIgnoredError,
    StoreQueryRotatedError,
)


def _grid(products, total, *, offset=0, is_last=False, facets=None):
    grid = {
        "products": products,
        "pageInfo": {"totalCount": total, "offset": offset, "size": len(products), "isLast": is_last},
    }
    if facets is not None:
        grid["facetOptions"] = [
            {"name": name, "values": [{"key": key, "count": count} for key, count in values.items()]}
            for name, values in facets.items()
        ]
    return {"data": {"categoryGridRetrieve": grid}}


_CLASSIFICATION_FACETS = {"storeDisplayClassification": {"FULL_GAME": 6952, "GAME_BUNDLE": 1397}}


def _product(
    product_id="P1", name="Bloodborne", platforms=("PS4",), np_title_id="CUSA00207", media=None, cls="Full Game"
):
    """A product in the shape the live gateway actually returns."""
    return {
        "__typename": "Product",
        "id": product_id,
        "name": name,
        "platforms": list(platforms),
        "npTitleId": np_title_id,
        "localizedStoreDisplayClassification": cls,
        "media": media if media is not None else [{"role": "GAMEHUB_COVER_ART", "type": "IMAGE", "url": "cover.jpg"}],
    }


def _client(handler):
    return StoreCatalogClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_reads_a_page_and_the_category_total():
    def handler(request):
        return httpx.Response(
            200,
            json=_grid(
                [
                    _product("P1", "Bloodborne", ("PS4",), "CUSA00207"),
                    _product("P2", "Returnal", ("PS5",), "PPSA01342"),
                ],
                7604,
            ),
        )

    page = await _client(handler).category_page("cat-1")

    assert page.total_count == 7604
    assert [p.product_id for p in page.products] == ["P1", "P2"]
    assert page.products[0].platforms == ("PS4",)


async def test_carries_the_np_title_id_that_joins_onto_existing_curator_rows():
    def handler(request):
        return httpx.Response(200, json=_grid([_product(np_title_id="CUSA00207")], 1))

    page = await _client(handler).category_page("cat-1")

    assert page.products[0].np_title_id == "CUSA00207", (
        "npTitleId is what makes a backfilled product joinable to library_entries/psn_catalog_cache"
    )


async def test_resolves_cover_art_by_role_preference_not_array_order():
    media = [
        {"role": "SCREENSHOT", "type": "IMAGE", "url": "shot.jpg"},
        {"role": "PREVIEW", "type": "VIDEO", "url": "clip.mp4"},
        {"role": "GAMEHUB_COVER_ART", "type": "IMAGE", "url": "cover.jpg"},
    ]

    def handler(request):
        return httpx.Response(200, json=_grid([_product(media=media)], 1))

    page = await _client(handler).category_page("cat-1")

    assert page.products[0].cover_image_url == "cover.jpg", "a screenshot listed first must not win"


async def test_falls_back_through_the_role_preference_and_ignores_video():
    def handler(request):
        return httpx.Response(
            200,
            json=_grid(
                [
                    _product(
                        media=[
                            {"role": "PREVIEW", "type": "VIDEO", "url": "clip.mp4"},
                            {"role": "PORTRAIT_BANNER", "type": "IMAGE", "url": "portrait.jpg"},
                        ]
                    )
                ],
                1,
            ),
        )

    page = await _client(handler).category_page("cat-1")

    assert page.products[0].cover_image_url == "portrait.jpg"


async def test_a_product_with_no_usable_image_reports_none_rather_than_a_video_url():
    def handler(request):
        return httpx.Response(
            200, json=_grid([_product(media=[{"role": "PREVIEW", "type": "VIDEO", "url": "clip.mp4"}])], 1)
        )

    page = await _client(handler).category_page("cat-1")

    assert page.products[0].cover_image_url is None


async def test_distinguishes_full_games_from_add_ons():
    def handler(request):
        return httpx.Response(
            200,
            json=_grid([_product("P1", cls="Full Game"), _product("P2", cls="Add-On")], 2),
        )

    page = await _client(handler).category_page("cat-1")

    assert page.products[0].is_full_game is True
    assert page.products[1].is_full_game is False


async def test_reports_is_last_so_a_walk_terminates_on_the_gateway_not_on_a_drifting_total():
    def handler(request):
        return httpx.Response(200, json=_grid([_product()], 7165, offset=7160, is_last=True))

    page = await _client(handler).category_page("cat-1", offset=7160)

    assert page.is_last is True
    assert page.offset == 7160


async def test_calls_the_storefront_gateway_not_the_authenticated_mobile_one():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_grid([], 0))

    await _client(handler).category_page("cat-1")

    assert seen["url"].startswith(STORE_GRAPHQL_URL)
    assert "m.np.playstation.com" not in seen["url"], (
        "the mobile gateway needs a PSN token and cannot enumerate; this client exists to avoid it"
    )


async def test_sends_no_credential_of_any_kind():
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        return httpx.Response(200, json=_grid([], 0))

    await _client(handler).category_page("cat-1")

    assert "authorization" not in seen["headers"], "the storefront gateway is anonymous by design"
    assert "cookie" not in seen["headers"]


async def test_sends_the_apollo_preflight_header_the_gateway_demands():
    seen = {}

    def handler(request):
        seen["headers"] = request.headers
        return httpx.Response(200, json=_grid([], 0))

    await _client(handler).category_page("cat-1")

    assert seen["headers"]["apollo-require-preflight"] == "true", (
        "without this the gateway rejects the call as a possible CSRF, which is not an auth failure"
    )
    assert seen["headers"]["x-apollo-operation-name"] == "categoryGridRetrieve"


async def test_paging_arguments_reach_the_gateway():
    seen = {}

    def handler(request):
        seen["variables"] = json.loads(request.url.params["variables"])
        return httpx.Response(200, json=_grid([], 0))

    await _client(handler).category_page("cat-9", offset=300, size=50)

    assert seen["variables"]["id"] == "cat-9"
    assert seen["variables"]["pageArgs"] == {"size": 50, "offset": 300}


async def test_a_rotated_persisted_query_hash_is_its_own_error():
    def handler(request):
        return httpx.Response(400, json={"message": "Query 000000 not whitelisted"})

    with pytest.raises(StoreQueryRotatedError) as excinfo:
        await _client(handler).category_page("cat-1")

    assert "refresh it" in str(excinfo.value), "the message must point at the fix, not just report failure"


async def test_a_rotated_hash_falls_through_to_the_next_candidate():
    tried = []

    def handler(request):
        sha = json.loads(request.url.params["extensions"])["persistedQuery"]["sha256Hash"]
        tried.append(sha)
        if sha == "dead":
            return httpx.Response(400, json={"message": "Query dead not whitelisted"})
        return httpx.Response(200, json=_grid([_product()], 1, is_last=True))

    client = StoreCatalogClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), query_hashes=("dead", "live")
    )
    page = await client.category_page("cat-1")

    assert tried == ["dead", "live"], "candidates are tried in order, stopping at the first that works"
    assert len(page.products) == 1


async def test_the_rotated_error_only_surfaces_once_every_candidate_is_exhausted():
    def handler(request):
        return httpx.Response(400, json={"message": "Query x not whitelisted"})

    client = StoreCatalogClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), query_hashes=("one", "two"))

    with pytest.raises(StoreQueryRotatedError):
        await client.category_page("cat-1")


async def test_facet_census_returns_every_published_key_with_its_count():
    def handler(request):
        return httpx.Response(
            200, json=_grid([_product()], 7604, facets={"productGenres": {"SHOOTER": 812, "PUZZLE": 391}})
        )

    census = await _client(handler).facet_census("cat-1", "productGenres")

    assert census == {"SHOOTER": 812, "PUZZLE": 391}


async def test_facet_census_is_none_when_the_category_publishes_no_such_facet():
    def handler(request):
        return httpx.Response(200, json=_grid([_product()], 143, facets={"targetPlatforms": {"PS5": 143}}))

    census = await _client(handler).facet_census("cat-1", "productGenres")

    assert census is None


async def test_facet_census_costs_one_product_page_because_the_census_spans_the_category():
    seen = {}

    def handler(request):
        seen["variables"] = json.loads(request.url.params["variables"])
        return httpx.Response(200, json=_grid([_product()], 7604, facets={"productGenres": {"SHOOTER": 812}}))

    await _client(handler).facet_census("cat-1", "productGenres")

    assert seen["variables"]["pageArgs"] == {"size": 1, "offset": 0}
    assert seen["variables"]["filterBy"] == []


async def test_facet_census_shares_the_hash_rotation_rather_than_reimplementing_it():
    tried = []

    def handler(request):
        sha = json.loads(request.url.params["extensions"])["persistedQuery"]["sha256Hash"]
        tried.append(sha)
        if sha == "dead":
            return httpx.Response(400, json={"message": "Query dead not whitelisted"})
        return httpx.Response(200, json=_grid([_product()], 1, facets={"productGenres": {"SHOOTER": 1}}))

    client = StoreCatalogClient(
        httpx.AsyncClient(transport=httpx.MockTransport(handler)), query_hashes=("dead", "live")
    )
    census = await client.facet_census("cat-1", "productGenres")

    assert tried == ["dead", "live"]
    assert census == {"SHOOTER": 1}


async def test_walks_in_ascending_release_date_so_a_new_release_cannot_shift_the_walk():
    seen = {}

    def handler(request):
        seen["variables"] = json.loads(request.url.params["variables"])
        return httpx.Response(200, json=_grid([], 0))

    await _client(handler).category_page("cat-1")

    assert seen["variables"]["sortBy"] == {"name": "productReleaseDate", "isAscending": True}


async def test_requested_filters_reach_the_gateway():
    seen = {}

    def handler(request):
        seen["variables"] = json.loads(request.url.params["variables"])
        return httpx.Response(200, json=_grid([], 6952, facets=_CLASSIFICATION_FACETS))

    await _client(handler).category_page("cat-1", filter_by=(FULL_GAME_FILTER,))

    assert seen["variables"]["filterBy"] == ["storeDisplayClassification:FULL_GAME"]


async def test_a_filter_that_narrowed_to_its_facet_count_is_accepted():
    def handler(request):
        return httpx.Response(200, json=_grid([_product()], 6952, facets=_CLASSIFICATION_FACETS))

    page = await _client(handler).category_page("cat-1", filter_by=(FULL_GAME_FILTER,))

    assert page.total_count == 6952


async def test_a_silently_ignored_filter_is_rejected_rather_than_trusted():
    def handler(request):
        return httpx.Response(200, json=_grid([_product()], 9190, facets=_CLASSIFICATION_FACETS))

    with pytest.raises(StoreFilterIgnoredError) as excinfo:
        await _client(handler).category_page("cat-1", filter_by=(FULL_GAME_FILTER,))

    message = str(excinfo.value)
    assert "6952" in message, "the message must name what the category says the facet holds"
    assert "9190" in message, "the message must name what the filtered query actually returned"


async def test_a_filter_matching_nothing_is_rejected_rather_than_ending_the_walk():
    def handler(request):
        return httpx.Response(200, json=_grid([], 0, is_last=True, facets=_CLASSIFICATION_FACETS))

    with pytest.raises(StoreFilterIgnoredError) as excinfo:
        await _client(handler).category_page("cat-1", filter_by=("storeDisplayClassification:NOT_REAL",))

    assert "publishes no 'NOT_REAL' value" in str(excinfo.value)


async def test_a_zero_result_on_a_published_key_is_rejected_too():
    def handler(request):
        return httpx.Response(200, json=_grid([], 0, is_last=True, facets=_CLASSIFICATION_FACETS))

    with pytest.raises(StoreFilterIgnoredError):
        await _client(handler).category_page("cat-1", filter_by=(FULL_GAME_FILTER,))


async def test_an_unfiltered_page_is_never_checked_against_facets():
    def handler(request):
        return httpx.Response(200, json=_grid([_product()], 9190, facets=_CLASSIFICATION_FACETS))

    page = await _client(handler).category_page("cat-1")

    assert page.total_count == 9190


async def test_a_category_publishing_no_census_for_the_facet_is_allowed_through():
    def handler(request):
        return httpx.Response(200, json=_grid([_product()], 143, facets={"targetPlatforms": {"PS5": 143}}))

    page = await _client(handler).category_page("cat-1", filter_by=(FULL_GAME_FILTER,))

    assert page.total_count == 143


async def test_a_response_without_any_facets_cannot_disprove_the_filter_so_is_allowed():
    def handler(request):
        return httpx.Response(200, json=_grid([_product()], 6952))

    page = await _client(handler).category_page("cat-1", filter_by=(FULL_GAME_FILTER,))

    assert page.total_count == 6952


async def test_other_graphql_errors_are_not_reported_as_a_rotated_hash():
    def handler(request):
        return httpx.Response(200, json={"errors": [{"message": "Category not found"}]})

    with pytest.raises(StoreCatalogError) as excinfo:
        await _client(handler).category_page("cat-1")

    assert not isinstance(excinfo.value, StoreQueryRotatedError)


async def test_a_server_error_is_surfaced_without_parsing_the_body():
    def handler(request):
        return httpx.Response(503, text="<html>upstream</html>")

    with pytest.raises(StoreCatalogError):
        await _client(handler).category_page("cat-1")


async def test_the_csrf_rejection_is_not_mistaken_for_a_rotated_hash():
    def handler(request):
        return httpx.Response(
            400,
            json={"errors": [{"message": "This operation has been blocked as a potential Cross-Site Request Forgery"}]},
        )

    with pytest.raises(StoreCatalogError) as excinfo:
        await _client(handler).category_page("cat-1")

    assert not isinstance(excinfo.value, StoreQueryRotatedError)


async def test_products_without_an_id_are_skipped_rather_than_crashing_the_walk():
    def handler(request):
        return httpx.Response(200, json=_grid([{"name": "Placeholder"}, _product("P2", "Real")], 2))

    page = await _client(handler).category_page("cat-1")

    assert [p.product_id for p in page.products] == ["P2"]


async def test_an_empty_page_is_not_an_error():
    def handler(request):
        return httpx.Response(200, json=_grid([], 0, offset=99999, is_last=True))

    page = await _client(handler).category_page("cat-1", offset=99999)

    assert page.products == ()
    assert page.total_count == 0
    assert page.offset == 99999
    assert page.is_last is True


async def test_offset_falls_back_to_the_request_when_the_gateway_omits_it():
    def handler(request):
        return httpx.Response(200, json={"data": {"categoryGridRetrieve": {"products": [], "pageInfo": {}}}})

    page = await _client(handler).category_page("cat-1", offset=250)

    assert page.offset == 250
