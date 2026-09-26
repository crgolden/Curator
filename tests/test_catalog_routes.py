"""Tests for GET /catalog/games, using create_app() with a hand-written fake CatalogRepository.

Reuses test_routes.py's fakes/helpers (FakeRepository, FakeTokenValidator, _claims, _bearer,
_make_settings) the same way test_authz.py does.
"""

from __future__ import annotations

import random
from dataclasses import replace
from typing import get_args

import httpx
from fastapi.testclient import TestClient

from curator import catalog_routes
from curator.app import create_app
from curator.catalog.content_kind import EVERY_KIND, GAME_KIND
from curator.catalog.ps_plus_walk_service import CATEGORY_RENAMED, PsPlusWalkProgress
from curator.catalog.repository import (
    GENRE_VOCABULARY_SQL,
    CatalogPage,
    CatalogPrice,
    CatalogRepository,
    CatalogSortField,
    GameSummary,
    PublicCollectionSummary,
)
from curator.catalog.store_backfill_service import (
    NO_PRODUCTS,
    PAGE_BUDGET_EXHAUSTED,
    BackfillProgress,
    BackfillSummary,
)
from curator.catalog_routes import (
    ALL_CATEGORIES_EMPTY_MESSAGE,
    DEFAULT_GAMES_LIMIT,
    EXCLUDE_OWNED_PARAM,
    MAX_GAMES_LIMIT,
    PUBLIC_COLLECTIONS_LIMIT,
    BackfillRejectedDetail,
    CatalogBackfillRequest,
    CatalogBackfillResponse,
    CatalogGamesResponse,
    CatalogGenreDriftResponse,
    CatalogGenresResponse,
    GameCollectionsResponse,
    GameSummaryResponse,
    PsPlusWalkRequest,
    PsPlusWalkResponse,
)
from curator.persistence.crypto import TokenCrypto
from curator.psn._graphql import DATA_KEY
from curator.psn.store_client import (
    CATEGORY_GRID_RETRIEVE_OPERATION,
    FACET_NAME_KEY,
    FACET_OPTIONS_KEY,
    FACET_VALUE_COUNT_KEY,
    FACET_VALUE_KEY_KEY,
    FACET_VALUES_KEY,
    IS_LAST_KEY,
    OFFSET_KEY,
    PAGE_INFO_KEY,
    PRODUCT_GENRES_FACET,
    PRODUCTS_KEY,
    SIZE_KEY,
    TOTAL_COUNT_KEY,
    StoreCatalogClient,
)
from curator.query_params import (
    AAA_TIER_PARAM,
    CATEGORY_ID_PARAM,
    FRANCHISE_PARAM,
    GENRE_PARAM,
    LIMIT_PARAM,
    OFFSET_PARAM,
    SORT_DIR_PARAM,
    SORT_PARAM,
)
from test_catalog_repository import FakePool
from test_routes import (
    FakeAgentFactory,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _path,
    _seed_link,
)
from test_values import (
    new_category_id,
    new_definition_id,
    new_facet_key,
    new_game_id,
    new_game_title,
    new_genre_name,
    new_non_game_kind,
    new_opaque_token,
    new_percent_completed,
    new_positive_count,
    new_price_cents,
    new_ps_plus_tier,
    new_psn_rating,
    new_review_score,
    new_share_slug,
    new_utc_instant,
    new_walk_id,
    token_from_first_half_of_alphabet,
    token_from_second_half_of_alphabet,
)


def _facet_keys() -> tuple[str, ...]:
    return tuple(new_facet_key() for _ in range(new_positive_count(20)))


def _facet_page(facet_name, keys):
    return {
        DATA_KEY: {
            CATEGORY_GRID_RETRIEVE_OPERATION: {
                PRODUCTS_KEY: [],
                PAGE_INFO_KEY: {TOTAL_COUNT_KEY: len(keys), OFFSET_KEY: 0, SIZE_KEY: 1, IS_LAST_KEY: False},
                FACET_OPTIONS_KEY: [
                    {
                        FACET_NAME_KEY: facet_name,
                        FACET_VALUES_KEY: [{FACET_VALUE_KEY_KEY: key, FACET_VALUE_COUNT_KEY: 1} for key in keys],
                    },
                ],
            }
        }
    }


def _genre_facet_response(keys):
    return _facet_page(PRODUCT_GENRES_FACET, keys)


def _store_client(payload):
    def handler(_request):
        return httpx.Response(200, json=payload)

    return StoreCatalogClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


class FakeCatalogRepository:
    def __init__(self, games=None, genres=None, genre_vocabulary=None, excluded_owned=0):
        self._games = games or []
        self._genres = genres or []
        self._genre_vocabulary = list(genre_vocabulary or [])
        self._excluded_owned = excluded_owned
        self.list_games_calls = []
        self.get_game_calls = []
        self.kind_calls: list[str | None] = []
        self.sort_calls: list[tuple[str, str]] = []
        self.public_collections: list[PublicCollectionSummary] = []
        self.collections_calls: list[tuple[str, int]] = []

    async def list_genre_vocabulary(self):
        return list(self._genre_vocabulary)

    async def list_games(
        self,
        *,
        search=None,
        franchise=None,
        genre=None,
        aaa_tier=None,
        exclude_owned_by=None,
        kind=None,
        sort="title",
        sort_dir="asc",
        limit=50,
        offset=0,
    ):
        self.list_games_calls.append((search, franchise, genre, aaa_tier, exclude_owned_by, limit, offset))
        self.kind_calls.append(kind)
        self.sort_calls.append((sort, sort_dir))
        return CatalogPage(games=self._games, total=len(self._games), excluded_owned=self._excluded_owned)

    async def list_public_collections_containing(self, game_id, *, limit=20):
        self.collections_calls.append((game_id, limit))
        return list(self.public_collections[:limit]), len(self.public_collections)

    async def get_game(self, game_id, identity_sub=None):
        self.get_game_calls.append((game_id, identity_sub))
        return next((game for game in self._games if game.game_id == game_id), None)

    async def list_genres(self):
        return list(self._genres)


def _build(
    catalog_repository=None,
    *,
    backfill_service=None,
    omit_backfill_service=False,
    store_client=None,
    omit_store_client=False,
    repository=None,
):
    repository = repository if repository is not None else FakeRepository()
    token_crypto = TokenCrypto(TokenCrypto.generate_key())
    validator = FakeTokenValidator()
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=validator,
        catalog_repository=catalog_repository or FakeCatalogRepository(),
    )
    if omit_backfill_service:
        app.state.store_backfill_service = None
    elif backfill_service is not None:
        app.state.store_backfill_service = backfill_service
    if omit_store_client:
        app.state.store_catalog_client = None
    elif store_client is not None:
        app.state.store_catalog_client = store_client
    return TestClient(app), validator


def _signed_in(validator, **claims_overrides) -> tuple[dict[str, str], str]:
    token = new_opaque_token()
    claims = _claims(**claims_overrides)
    validator.register(token, claims)
    return _bearer(token), claims.sub


def _admin(validator) -> dict[str, str]:
    headers, _sub = _signed_in(validator, is_admin=True)
    return headers


class FakeBackfillService:
    def __init__(self, summary=None):
        self.calls: list[tuple[list[str], int | None]] = []
        self.start_offsets: list[dict[str, int]] = []
        self._summary = summary or BackfillSummary()

    async def backfill(self, category_ids, *, max_pages_per_category=None, start_offsets=None):
        self.calls.append((list(category_ids), max_pages_per_category))
        self.start_offsets.append(dict(start_offsets or {}))
        return self._summary


def _progress(**overrides):
    return replace(
        BackfillProgress(
            category_id=new_category_id(),
            next_offset=new_positive_count(),
            completed=False,
            pages_read=new_positive_count(),
            products_seen=new_positive_count(),
            games_created=new_positive_count(),
            covers_cached=new_positive_count(),
            stopped_reason=None,
        ),
        **overrides,
    )


def _game(**overrides):
    return replace(
        GameSummary(
            game_id=new_game_id(),
            canonical_title=new_game_title(),
            franchise=None,
            genre=None,
            aaa_tier=None,
        ),
        **overrides,
    )


def _backfill_body(category_ids, **fields) -> dict[str, object]:
    return CatalogBackfillRequest(category_ids=category_ids, **fields).model_dump(exclude_unset=True)


def _games(response) -> CatalogGamesResponse:
    return CatalogGamesResponse.model_validate(response.json())


def test_browsing_the_catalog_needs_no_token():
    game = _game()
    client, _validator = _build(FakeCatalogRepository([game]))

    response = client.get(_path(client, catalog_routes.list_games))

    assert response.status_code == 200
    assert _games(response).games[0].canonical_title == game.canonical_title


def test_exclude_owned_is_ignored_for_an_anonymous_caller_who_has_no_library():
    catalog_repository = FakeCatalogRepository([_game()])
    client, _validator = _build(catalog_repository)

    response = client.get(_path(client, catalog_routes.list_games), params={EXCLUDE_OWNED_PARAM: True})

    assert response.status_code == 200
    assert catalog_repository.list_games_calls[0][4] is None


def test_exclude_owned_passes_the_callers_sub_so_the_database_does_the_subtraction():
    catalog_repository = FakeCatalogRepository([_game()])
    client, validator = _build(catalog_repository)
    headers, sub = _signed_in(validator)

    response = client.get(_path(client, catalog_routes.list_games), params={EXCLUDE_OWNED_PARAM: True}, headers=headers)

    assert response.status_code == 200
    assert catalog_repository.list_games_calls[0][4] == sub


def test_the_response_says_how_many_matches_the_callers_library_removed():
    """An empty page has two meanings -- no such game, or you already own them all -- and a client that
    cannot tell them apart has to make a second request and decide for itself, which puts the rule in the
    browser. The count is the server answering the question."""
    excluded_owned = new_positive_count()
    catalog_repository = FakeCatalogRepository(excluded_owned=excluded_owned)
    client, validator = _build(catalog_repository)
    headers, _sub = _signed_in(validator)

    response = client.get(_path(client, catalog_routes.list_games), params={EXCLUDE_OWNED_PARAM: True}, headers=headers)

    assert response.status_code == 200
    assert _games(response).excluded_owned == excluded_owned


def test_omitting_exclude_owned_leaves_a_signed_in_browse_unfiltered():
    catalog_repository = FakeCatalogRepository([_game()])
    client, validator = _build(catalog_repository)
    headers, _sub = _signed_in(validator)

    response = client.get(_path(client, catalog_routes.list_games), headers=headers)

    assert response.status_code == 200
    assert catalog_repository.list_games_calls[0][4] is None


def test_catalog_carries_ratings_and_the_derived_tier():
    game = _game(
        aaa_tier=new_genre_name(),
        critical_score=new_review_score(),
        oc_score=new_review_score(),
        psn_rating=new_psn_rating(),
    )
    client, _validator = _build(FakeCatalogRepository([game]))

    served = _games(client.get(_path(client, catalog_routes.list_games))).games[0]

    assert served.critical_score == game.critical_score
    assert served.oc_score == game.oc_score
    assert served.psn_rating == game.psn_rating
    assert served.aaa_tier == game.aaa_tier


def test_listing_the_genre_filter_options_needs_no_token():
    genres = [new_genre_name(), new_genre_name()]
    client, _validator = _build(FakeCatalogRepository(genres=genres))

    response = client.get(_path(client, catalog_routes.list_genres))

    assert response.status_code == 200
    assert CatalogGenresResponse.model_validate(response.json()).genres == genres


def test_genres_keep_the_curation_priority_order_rather_than_being_sorted_alphabetically():
    priority_order = [token_from_second_half_of_alphabet(), token_from_first_half_of_alphabet()]
    client, _validator = _build(FakeCatalogRepository(genres=priority_order))

    response = client.get(_path(client, catalog_routes.list_genres))

    assert CatalogGenresResponse.model_validate(response.json()).genres == priority_order


def test_a_catalog_with_no_enriched_games_offers_no_genres_rather_than_erroring():
    client, _validator = _build(FakeCatalogRepository(genres=[]))

    response = client.get(_path(client, catalog_routes.list_genres))

    assert response.status_code == 200
    assert CatalogGenresResponse.model_validate(response.json()).genres == []


def test_genre_drift_reports_a_live_facet_key_the_genres_table_has_never_heard_of():
    seeded = _facet_keys()
    unseeded_facet_key = new_facet_key()
    client, validator = _build(
        FakeCatalogRepository(genre_vocabulary=seeded),
        store_client=_store_client(_genre_facet_response((*seeded, unseeded_facet_key))),
    )

    response = client.get(_path(client, catalog_routes.genre_vocabulary_drift), headers=_admin(validator))

    assert response.status_code == 200
    drift = CatalogGenreDriftResponse.model_validate(response.json())
    assert drift.missing_from_table == [unseeded_facet_key]
    assert drift.missing_from_facet == []
    assert drift.matched == len(set(seeded))


def test_genre_drift_reports_nothing_when_the_facet_matches_the_seeded_vocabulary_exactly():
    seeded = _facet_keys()
    client, validator = _build(
        FakeCatalogRepository(genre_vocabulary=seeded),
        store_client=_store_client(_genre_facet_response(seeded)),
    )

    response = client.get(_path(client, catalog_routes.genre_vocabulary_drift), headers=_admin(validator))

    drift = CatalogGenreDriftResponse.model_validate(response.json())
    assert drift.missing_from_table == []
    assert drift.missing_from_facet == []
    assert drift.matched == len(set(seeded))


def test_genre_drift_reports_a_seeded_genre_the_storefront_has_stopped_publishing():
    still_published = _facet_keys()
    retired_genre = new_facet_key()
    client, validator = _build(
        FakeCatalogRepository(genre_vocabulary=(*still_published, retired_genre)),
        store_client=_store_client(_genre_facet_response(still_published)),
    )

    response = client.get(_path(client, catalog_routes.genre_vocabulary_drift), headers=_admin(validator))

    drift = CatalogGenreDriftResponse.model_validate(response.json())
    assert drift.missing_from_facet == [retired_genre]
    assert drift.missing_from_table == []


def test_genre_drift_reads_the_genres_table_once_and_writes_nothing():
    seeded = _facet_keys()
    pool = FakePool(fetchall_results=[[(key,) for key in seeded]])
    client, validator = _build(
        CatalogRepository(pool),
        store_client=_store_client(_genre_facet_response(seeded)),
    )

    client.get(_path(client, catalog_routes.genre_vocabulary_drift), headers=_admin(validator))

    executed = [sql for conn in pool.connections for sql, _params in conn.executed]
    assert executed == [GENRE_VOCABULARY_SQL]


def test_genre_drift_rejects_a_category_that_publishes_no_product_genres_facet():
    no_genre_facet = _facet_page(new_facet_key(), _facet_keys())
    client, validator = _build(
        FakeCatalogRepository(genre_vocabulary=_facet_keys()),
        store_client=_store_client(no_genre_facet),
    )

    response = client.get(
        _path(client, catalog_routes.genre_vocabulary_drift),
        params={CATEGORY_ID_PARAM: new_category_id()},
        headers=_admin(validator),
    )

    assert response.status_code == 502, "an empty delta here would read as 'no drift' when nothing was compared"


def test_genre_drift_requires_admin_not_merely_a_bearer_token():
    client, validator = _build(store_client=_store_client(_genre_facet_response(_facet_keys())))
    headers, _sub = _signed_in(validator)

    assert client.get(_path(client, catalog_routes.genre_vocabulary_drift), headers=headers).status_code == 403


def test_genre_drift_is_not_anonymous_like_the_rest_of_the_catalog_routes():
    client, _validator = _build(store_client=_store_client(_genre_facet_response(_facet_keys())))

    assert client.get(_path(client, catalog_routes.genre_vocabulary_drift)).status_code == 401


def test_genre_drift_returns_503_when_the_store_client_is_not_configured():
    client, validator = _build(omit_store_client=True)

    response = client.get(_path(client, catalog_routes.genre_vocabulary_drift), headers=_admin(validator))

    assert response.status_code == 503


def test_genre_drift_returns_503_when_the_store_client_was_never_set():
    client, validator = _build()
    del client.app.state.store_catalog_client

    response = client.get(_path(client, catalog_routes.genre_vocabulary_drift), headers=_admin(validator))

    assert response.status_code == 503


def test_backfill_still_requires_admin_now_that_browsing_is_anonymous():
    client, _validator = _build()

    response = client.post(_path(client, catalog_routes.backfill_catalog), json=_backfill_body([new_category_id()]))

    assert response.status_code == 401


def test_backfill_requires_admin_not_merely_a_bearer_token():
    client, validator = _build()
    headers, _sub = _signed_in(validator)

    response = client.post(
        _path(client, catalog_routes.backfill_catalog), json=_backfill_body([new_category_id()]), headers=headers
    )

    assert response.status_code == 403


def test_backfill_reports_progress_and_totals():
    finished = _progress(completed=True)
    unfinished = _progress(completed=False, stopped_reason=PAGE_BUDGET_EXHAUSTED)
    service = FakeBackfillService(BackfillSummary(categories=[finished, unfinished]))
    client, validator = _build(backfill_service=service)
    category_ids = [finished.category_id, unfinished.category_id]
    max_pages = new_positive_count()

    response = client.post(
        _path(client, catalog_routes.backfill_catalog),
        json=_backfill_body(category_ids, max_pages_per_category=max_pages),
        headers=_admin(validator),
    )

    assert response.status_code == 200
    body = CatalogBackfillResponse.model_validate(response.json())
    assert body.completed is False, "one unfinished category means the run is unfinished"
    assert body.games_created == finished.games_created + unfinished.games_created
    assert body.covers_cached == finished.covers_cached + unfinished.covers_cached
    assert body.categories[1].stopped_reason == PAGE_BUDGET_EXHAUSTED
    assert body.categories[1].next_offset == unfinished.next_offset, "the caller resumes from here"
    assert service.calls == [(category_ids, max_pages)]


def test_an_all_empty_backfill_422s_but_still_returns_what_each_category_did():
    """A bare detail string tells the caller the request failed and nothing about which id was wrong or
    how far each walk got. The 422 carries the same per-category rows a 2xx would."""
    empty = [_progress(products_seen=0, games_created=0, stopped_reason=NO_PRODUCTS) for _ in range(2)]
    client, validator = _build(backfill_service=FakeBackfillService(BackfillSummary(categories=empty)))
    category_ids = [progress.category_id for progress in empty]

    response = client.post(
        _path(client, catalog_routes.backfill_catalog), json=_backfill_body(category_ids), headers=_admin(validator)
    )

    assert response.status_code == 422
    detail = BackfillRejectedDetail.model_validate(response.json()["detail"])
    assert detail.message == ALL_CATEGORIES_EMPTY_MESSAGE
    assert [row.category_id for row in detail.categories] == category_ids
    assert [row.stopped_reason for row in detail.categories] == [NO_PRODUCTS, NO_PRODUCTS]
    assert detail.categories[0].pages_read == empty[0].pages_read


def test_reading_one_game_needs_no_token():
    game = _game(psn_rating=new_psn_rating())
    client, _validator = _build(FakeCatalogRepository([game]))

    response = client.get(_path(client, catalog_routes.get_game, game_id=game.game_id))

    assert response.status_code == 200
    served = GameSummaryResponse.model_validate(response.json())
    assert served.canonical_title == game.canonical_title
    assert served.psn_rating == game.psn_rating


def test_an_anonymous_visitor_gets_no_trophy_progress_on_a_game_page():
    game = _game()
    catalog_repository = FakeCatalogRepository([game])
    client, _validator = _build(catalog_repository)

    response = client.get(_path(client, catalog_routes.get_game, game_id=game.game_id))

    assert response.status_code == 200
    assert GameSummaryResponse.model_validate(response.json()).percent_completed is None
    assert catalog_repository.get_game_calls == [(game.game_id, None)]


def test_a_signed_in_caller_harvesting_trophies_gets_their_own_progress_on_a_game_page():
    game = _game(percent_completed=new_percent_completed())
    catalog_repository = FakeCatalogRepository([game])
    repository = FakeRepository()
    client, validator = _build(catalog_repository, repository=repository)
    headers, sub = _signed_in(validator)
    _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), sub, harvest_trophies=True)

    response = client.get(_path(client, catalog_routes.get_game, game_id=game.game_id), headers=headers)

    assert response.status_code == 200
    assert GameSummaryResponse.model_validate(response.json()).percent_completed == game.percent_completed
    assert catalog_repository.get_game_calls == [(game.game_id, sub)]


def test_a_signed_in_caller_with_trophy_harvesting_off_is_served_a_game_page_without_progress():
    game = _game(percent_completed=new_percent_completed())
    catalog_repository = FakeCatalogRepository([game])
    repository = FakeRepository()
    client, validator = _build(catalog_repository, repository=repository)
    headers, sub = _signed_in(validator)
    _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), sub, harvest_trophies=False)

    response = client.get(_path(client, catalog_routes.get_game, game_id=game.game_id), headers=headers)

    assert response.status_code == 200
    assert catalog_repository.get_game_calls == [(game.game_id, None)]


def test_a_game_page_rejects_a_supplied_token_that_is_invalid_rather_than_serving_it_anonymously():
    game = _game()
    client, _validator = _build(FakeCatalogRepository([game]))

    response = client.get(
        _path(client, catalog_routes.get_game, game_id=game.game_id), headers=_bearer(new_opaque_token())
    )

    assert response.status_code == 401


def test_reading_an_unknown_game_is_a_404_not_an_empty_body():
    client, _validator = _build(FakeCatalogRepository([]))

    response = client.get(_path(client, catalog_routes.get_game, game_id=new_game_id()))

    assert response.status_code == 404


def test_backfill_resumes_a_category_from_the_offset_a_previous_run_reported():
    service = FakeBackfillService()
    client, validator = _build(backfill_service=service)
    category_id = new_category_id()
    start_offsets = {category_id: new_positive_count()}

    client.post(
        _path(client, catalog_routes.backfill_catalog),
        json=_backfill_body([category_id], start_offsets=start_offsets),
        headers=_admin(validator),
    )

    assert service.start_offsets == [start_offsets]


def test_backfill_starts_from_zero_when_no_offset_is_given():
    service = FakeBackfillService()
    client, validator = _build(backfill_service=service)

    client.post(
        _path(client, catalog_routes.backfill_catalog),
        json=_backfill_body([new_category_id()]),
        headers=_admin(validator),
    )

    assert service.start_offsets == [{}]


def test_backfill_returns_503_when_the_store_client_is_not_configured():
    client, validator = _build(backfill_service=None, omit_backfill_service=True)

    response = client.post(
        _path(client, catalog_routes.backfill_catalog),
        json=_backfill_body([new_category_id()]),
        headers=_admin(validator),
    )

    assert response.status_code == 503


def test_backfill_returns_503_when_the_service_was_never_set():
    client, validator = _build()
    del client.app.state.store_backfill_service

    response = client.post(
        _path(client, catalog_routes.backfill_catalog),
        json=_backfill_body([new_category_id()]),
        headers=_admin(validator),
    )

    assert response.status_code == 503


def test_returns_games_from_repository():
    game = _game(franchise=new_game_title(), genre=new_genre_name(), aaa_tier=new_genre_name())
    client, validator = _build(FakeCatalogRepository(games=[game]))
    headers, _sub = _signed_in(validator)

    response = client.get(_path(client, catalog_routes.list_games), headers=headers)

    assert response.status_code == 200
    body = _games(response)
    assert body.games == [
        GameSummaryResponse(
            game_id=game.game_id,
            canonical_title=game.canonical_title,
            franchise=game.franchise,
            genre=game.genre,
            aaa_tier=game.aaa_tier,
        )
    ]
    assert body.total == 1


def test_passes_query_filters_through_to_repository():
    catalog_repository = FakeCatalogRepository()
    client, validator = _build(catalog_repository)
    headers, _sub = _signed_in(validator)
    franchise, genre, aaa_tier = new_game_title(), new_genre_name(), new_genre_name()
    limit, offset = new_positive_count(MAX_GAMES_LIMIT), new_positive_count()

    client.get(
        _path(client, catalog_routes.list_games),
        params={
            FRANCHISE_PARAM: franchise,
            GENRE_PARAM: genre,
            AAA_TIER_PARAM: aaa_tier,
            LIMIT_PARAM: limit,
            OFFSET_PARAM: offset,
        },
        headers=headers,
    )

    assert catalog_repository.list_games_calls == [(None, franchise, genre, aaa_tier, None, limit, offset)]


def test_title_search_reaches_the_repository():
    catalog_repository = FakeCatalogRepository()
    client, validator = _build(catalog_repository)
    headers, _sub = _signed_in(validator)
    term = new_genre_name()

    client.get(_path(client, catalog_routes.list_games), params={"q": term}, headers=headers)

    assert catalog_repository.list_games_calls == [(term, None, None, None, None, DEFAULT_GAMES_LIMIT, 0)]


def test_default_pagination():
    catalog_repository = FakeCatalogRepository()
    client, validator = _build(catalog_repository)
    headers, _sub = _signed_in(validator)

    client.get(_path(client, catalog_routes.list_games), headers=headers)

    assert catalog_repository.list_games_calls == [(None, None, None, None, None, DEFAULT_GAMES_LIMIT, 0)]


def test_browsing_leaves_proven_non_games_out_by_default_and_all_lifts_it():
    catalog_repository = FakeCatalogRepository()
    client, _validator = _build(catalog_repository)
    other_kind = new_non_game_kind()
    path = _path(client, catalog_routes.list_games)

    client.get(path)
    client.get(path, params={"kind": EVERY_KIND})
    client.get(path, params={"kind": other_kind})

    assert catalog_repository.kind_calls == [GAME_KIND, EVERY_KIND, other_kind]


def test_browsing_rejects_a_kind_outside_the_vocabulary():
    client, _validator = _build()

    assert client.get(_path(client, catalog_routes.list_games), params={"kind": new_genre_name()}).status_code == 422


def test_browsing_passes_the_price_sort_through():
    catalog_repository = FakeCatalogRepository()
    client, _validator = _build(catalog_repository)

    sort_field = random.choice(get_args(CatalogSortField))
    sort_direction = random.choice(("asc", "desc"))

    client.get(
        _path(client, catalog_routes.list_games), params={SORT_PARAM: sort_field, SORT_DIR_PARAM: sort_direction}
    )

    assert catalog_repository.sort_calls == [(sort_field, sort_direction)]


def test_browsing_rejects_a_sort_outside_the_allowlist():
    client, _validator = _build()

    assert client.get(_path(client, catalog_routes.list_games), params={"sort": new_genre_name()}).status_code == 422


def test_a_catalog_row_carries_its_content_kind_and_walked_price():
    price = CatalogPrice(
        is_free=False,
        tied_to_subscription=False,
        base_cents=new_price_cents(),
        discounted_cents=new_price_cents(),
        discount_text=new_genre_name(),
        fetched_at=new_utc_instant(),
    )
    priced = _game(content_kind=new_non_game_kind(), price=price)
    client, _validator = _build(FakeCatalogRepository([priced, _game()]))

    body = _games(client.get(_path(client, catalog_routes.list_games), params={"kind": EVERY_KIND}))

    assert body.games[0].content_kind == priced.content_kind
    assert body.games[0].price is not None
    assert body.games[0].price.base_cents == price.base_cents
    assert body.games[0].price.discounted_cents == price.discounted_cents
    assert body.games[1].content_kind is None
    assert body.games[1].price is None


def test_the_public_collections_of_a_game_need_no_token_and_list_what_the_repository_reports():
    catalog_repository = FakeCatalogRepository()
    listed = PublicCollectionSummary(
        definition_id=new_definition_id(),
        name=new_game_title(),
        share_slug=new_share_slug(),
        item_count=new_positive_count(),
        updated_at=new_utc_instant(),
    )
    catalog_repository.public_collections = [listed]
    client, _validator = _build(catalog_repository)
    game_id = new_game_id()

    response = client.get(_path(client, catalog_routes.get_game_collections, game_id=game_id))

    assert response.status_code == 200
    body = GameCollectionsResponse.model_validate(response.json())
    assert body.total == 1
    assert body.collections[0].share_slug == listed.share_slug
    assert body.collections[0].item_count == listed.item_count
    assert catalog_repository.collections_calls == [(game_id, PUBLIC_COLLECTIONS_LIMIT)]


class FakePsPlusWalkService:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[int | None] = []

    async def walk_all(self, *, max_pages_per_category=None):
        self.calls.append(max_pages_per_category)
        return self._results


def _ps_plus_progress(**overrides):
    distinct_products = new_positive_count()
    return replace(
        PsPlusWalkProgress(
            category_id=new_category_id(),
            tier=new_ps_plus_tier(),
            walk_id=new_walk_id(),
            pages_read=new_positive_count(),
            distinct_products=distinct_products,
            reported_total=distinct_products,
            stopped_reason=None,
            completed=True,
        ),
        **overrides,
    )


def _walk_body(**fields) -> dict[str, object]:
    return PsPlusWalkRequest(**fields).model_dump(exclude_unset=True)


def test_the_ps_plus_walk_requires_admin_not_merely_a_bearer_token():
    client, validator = _build()
    headers, _sub = _signed_in(validator)

    response = client.post(_path(client, catalog_routes.walk_ps_plus_catalog), json=_walk_body(), headers=headers)

    assert response.status_code == 403


def test_the_ps_plus_walk_reports_the_coverage_shortfall_that_decides_departures():
    shortfall = new_positive_count()
    distinct_products = new_positive_count()
    short = _ps_plus_progress(distinct_products=distinct_products, reported_total=distinct_products + shortfall)
    service = FakePsPlusWalkService([_ps_plus_progress(), short])
    client, validator = _build()
    client.app.state.ps_plus_walk_service = service
    max_pages = new_positive_count()

    response = client.post(
        _path(client, catalog_routes.walk_ps_plus_catalog),
        json=_walk_body(max_pages_per_category=max_pages),
        headers=_admin(validator),
    )

    assert response.status_code == 200
    body = PsPlusWalkResponse.model_validate(response.json())
    assert [row.coverage_shortfall for row in body.categories] == [0, shortfall]
    assert body.categories[1].tier == short.tier
    assert service.calls == [max_pages]


def test_the_ps_plus_walk_returns_503_when_the_walk_service_is_not_configured():
    client, validator = _build()
    client.app.state.ps_plus_walk_service = None

    response = client.post(
        _path(client, catalog_routes.walk_ps_plus_catalog), json=_walk_body(), headers=_admin(validator)
    )

    assert response.status_code == 503


def test_the_ps_plus_walk_returns_503_when_the_walk_service_was_never_set():
    client, validator = _build()
    del client.app.state.ps_plus_walk_service

    response = client.post(
        _path(client, catalog_routes.walk_ps_plus_catalog), json=_walk_body(), headers=_admin(validator)
    )

    assert response.status_code == 503


def test_the_ps_plus_walk_reports_a_renamed_category_as_stopped():
    service = FakePsPlusWalkService(
        [_ps_plus_progress(stopped_reason=CATEGORY_RENAMED, completed=False, distinct_products=0)]
    )
    client, validator = _build()
    client.app.state.ps_plus_walk_service = service

    response = client.post(
        _path(client, catalog_routes.walk_ps_plus_catalog), json=_walk_body(), headers=_admin(validator)
    )

    body = PsPlusWalkResponse.model_validate(response.json())
    assert body.categories[0].stopped_reason == CATEGORY_RENAMED
    assert body.categories[0].completed is False
