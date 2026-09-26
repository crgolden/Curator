"""Tests for GET /library/manual/candidates and GET /library/manual/search -- create_app wired with a
hand-written fake social_client_factory, mirroring test_devices_routes.py's style.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from audit_fakes import RecordingAuditRepository
from curator import library_routes
from curator.app import create_app
from curator.audit.repository import ACTION_STORE_SEARCH, OUTCOME_COMPLETED
from curator.catalog.repository import CatalogPage, GameSummary
from curator.catalog_routes import to_game_summary_response
from curator.deps import PREFERENCE_NOT_LINKED_DETAIL
from curator.library_routes import (
    INCLUDE_STORE_PARAM,
    MAX_STORE_SEARCH_LIMIT,
    SEARCH_DOMAIN_PARAM,
    SEARCH_LIMIT_PARAM,
    SEARCH_TERM_PARAM,
    STORE_UNAVAILABLE_NO_PSN_LINK,
    STORE_UNAVAILABLE_PSN_AUTH_FAILED,
    ManualCandidatesResponse,
    ManualGameRequest,
    ManualStoreHitRequest,
    StoreSearchResponse,
    StoreSearchResultResponse,
)
from curator.persistence.crypto import TokenCrypto
from curator.psn.errors import PsnAuthError
from curator.psn.models import GameSearchResult
from curator.psn.social_client import ADD_ONS_DOMAIN, FULL_GAMES_DOMAIN, MAX_GAME_SEARCH_PAGES
from curator.psn.title_platform import PS3, PS4, PS5
from test_routes import FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _path, _seed_link
from test_values import (
    lowercase_token,
    new_cover_image_url,
    new_game_id,
    new_game_title,
    new_identity_sub,
    new_opaque_token,
    new_positive_count,
    new_ps5_title_id,
    new_small_count,
    new_store_product_id,
)


class FakeSearchClient:
    """Stands in for SocialClient: records every search, returns canned hits, or raises when armed."""

    def __init__(self, *, results=(), raise_auth_error=False):
        self.results = list(results)
        self.raise_auth_error = raise_auth_error
        self.calls: list[tuple[str, str, int]] = []

    async def universal_search_games(self, query, *, domain=FULL_GAMES_DOMAIN, limit=20):
        self.calls.append((query, domain, limit))
        if self.raise_auth_error:
            raise PsnAuthError(lowercase_token())
        return self.results[:limit]


class FakeSearchClientFactory:
    """Raises ``RuntimeError`` for any ``sub`` not explicitly linked, as the real factory does."""

    def __init__(self):
        self.linked: dict[str, FakeSearchClient] = {}

    async def __call__(self, sub):
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(sub)
        return client


class FakeCatalogRepository:
    """Records every admission and catalog search, and answers the membership lookup from a fixed mapping."""

    def __init__(self, game_ids_by_store_id=None, admitted_game_id=None, catalog_games=(), excluded_owned=0):
        self._game_ids_by_store_id = dict(game_ids_by_store_id or {})
        self._admitted_game_id = admitted_game_id or new_game_id()
        self._catalog_games = list(catalog_games)
        self._excluded_owned = excluded_owned
        self.admitted: list[tuple[str, str, str | None, str | None]] = []
        self.linked: list[tuple[str, str, str | None, str | None]] = []
        self.looked_up: list[list[str]] = []
        self.searched: list[tuple[str | None, str | None, int]] = []

    async def game_ids_for_store_ids(self, store_ids):
        self.looked_up.append(list(store_ids))
        return {
            store_id: self._game_ids_by_store_id[store_id]
            for store_id in store_ids
            if store_id in self._game_ids_by_store_id
        }

    async def list_games(
        self,
        *,
        search=None,
        franchise=None,
        genre=None,
        aaa_tier=None,
        exclude_owned_by=None,
        limit=50,
        offset=0,
    ):
        self.searched.append((search, exclude_owned_by, limit))
        return CatalogPage(
            games=list(self._catalog_games),
            total=len(self._catalog_games),
            excluded_owned=self._excluded_owned,
        )

    async def admit_store_game(self, *, concept_id, name, product_id=None, cover_image_url=None):
        self.admitted.append((concept_id, name, product_id, cover_image_url))
        return self._admitted_game_id, True

    async def link_store_concept(self, game_id, *, concept_id, product_id, cover_image_url):
        self.linked.append((game_id, concept_id, product_id, cover_image_url))

    async def game_exists(self, game_id):
        return game_id in self._game_ids_by_store_id.values()

    async def title_id_for_game(self, game_id):
        return None


class FakeLibraryRepository:
    """Records manual upserts so a route test can assert what reached the library."""

    def __init__(self, written=True):
        self.manual_entries: list[tuple[str, str, tuple[str, ...], str | None]] = []
        self._written = written

    async def upsert_manual_entry(self, identity_sub, game_id, *, platforms, owned_edition):
        self.manual_entries.append((identity_sub, game_id, tuple(platforms), owned_edition))
        return self._written


class _Caller:
    def __init__(self) -> None:
        self.sub = new_identity_sub()
        self.token = new_opaque_token()


def _build(search_client=None, *, linked=True, catalog_repository=None, library_repository=None):
    caller = _Caller()
    validator = FakeTokenValidator()
    validator.register(caller.token, _claims(sub=caller.sub))
    factory = FakeSearchClientFactory()
    client = search_client if search_client is not None else FakeSearchClient()
    repository = FakeRepository()
    if linked:
        factory.linked[caller.sub] = client
        _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), caller.sub)
    app = create_app(
        _make_settings(),
        repository=repository,
        token_validator=validator,
        social_client_factory=factory,
        audit_repository=RecordingAuditRepository(),
    )
    app.state.catalog_repository = catalog_repository or FakeCatalogRepository()
    app.state.library_repository = library_repository or FakeLibraryRepository()
    return TestClient(app), caller, client


def _hit(name, store_id=None, *, platforms=(PS5,), default_product_id=None):
    return GameSearchResult(
        id=store_id or new_opaque_token(),
        kind=lowercase_token().capitalize(),
        default_product_id=default_product_id,
        name=name,
        platforms=platforms,
        cover_image_url=new_cover_image_url(),
        classification=new_game_title(),
        price=None,
        discounted_price=None,
        is_free=None,
    )


def _expected_store_row(hit: GameSearchResult, game_id: str | None) -> StoreSearchResultResponse:
    return StoreSearchResultResponse(
        id=hit.id,
        kind=hit.kind,
        game_id=game_id,
        default_product_id=hit.default_product_id,
        name=hit.name,
        platforms=list(hit.platforms),
        cover_image_url=hit.cover_image_url,
        classification=hit.classification,
        price=hit.price,
        discounted_price=hit.discounted_price,
        is_free=hit.is_free,
    )


def _catalogued(title):
    return GameSummary(
        game_id=new_game_id(),
        canonical_title=title,
        franchise=None,
        genre=None,
        aaa_tier=None,
    )


def _candidates_path(client: TestClient) -> str:
    return _path(client, library_routes.manual_add_candidates)


def _search_path(client: TestClient) -> str:
    return _path(client, library_routes.search_store_for_manual_add)


def _manual_path(client: TestClient) -> str:
    return _path(client, library_routes.add_manual_game)


def _store_hit_body(store_id: str, platforms: list[str] | None = None) -> dict[str, object]:
    store_hit = ManualStoreHitRequest(query=new_opaque_token(), id=store_id)
    if platforms is None:
        return ManualGameRequest(store_hit=store_hit).model_dump(exclude_unset=True)
    return ManualGameRequest(store_hit=store_hit, platforms=platforms).model_dump(exclude_unset=True)


def _candidates(response) -> ManualCandidatesResponse:
    return ManualCandidatesResponse.model_validate(response.json())


def _search_results(response) -> StoreSearchResponse:
    return StoreSearchResponse.model_validate(response.json())


def test_the_candidates_route_requires_a_bearer_token():
    client, _caller, _search = _build()

    response = client.get(_candidates_path(client), params={SEARCH_TERM_PARAM: new_opaque_token()})

    assert response.status_code == 401


def test_the_candidates_route_rejects_a_blank_search_term():
    client, caller, _search = _build()

    response = client.get(_candidates_path(client), params={SEARCH_TERM_PARAM: ""}, headers=_bearer(caller.token))

    assert response.status_code == 422


def test_a_catalog_answer_costs_the_caller_no_store_search():
    """A store search spends the caller's own PSN credentials, so it is never implicit on a term the
    catalog already answered."""
    expected_title = new_game_title()
    catalogued = _catalogued(expected_title)
    catalog = FakeCatalogRepository(catalog_games=[catalogued])
    client, caller, search = _build(catalog_repository=catalog)

    response = client.get(
        _candidates_path(client), params={SEARCH_TERM_PARAM: expected_title}, headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    candidates = _candidates(response)
    assert candidates.catalog == [to_game_summary_response(catalogued)]
    assert candidates.store == []
    assert candidates.store_consulted is False
    assert search.calls == []


def test_the_catalog_is_asked_to_leave_out_what_the_caller_already_holds():
    """The exclusion is the server's to apply: a client dropping owned rows from a returned page shrinks
    the page, hides addable games on later pages, and cannot see past the row cap at all."""
    searched_title = new_game_title()
    already_owned = new_positive_count()
    catalog = FakeCatalogRepository(excluded_owned=already_owned)
    client, caller, _search = _build(catalog_repository=catalog)

    response = client.get(
        _candidates_path(client), params={SEARCH_TERM_PARAM: searched_title}, headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    assert _candidates(response).already_owned == already_owned
    assert [(term, owner) for term, owner, _limit in catalog.searched] == [(searched_title, caller.sub)]


def test_owning_every_match_is_an_answer_rather_than_a_reason_to_search_the_store():
    """The remaining list is empty for a term the catalog knew perfectly well. Escalating would spend a
    PSN search to propose a game the caller already holds, which POST /library/manual answers 409 for."""
    already_owned = new_positive_count()
    catalog = FakeCatalogRepository(excluded_owned=already_owned)
    client, caller, search = _build(catalog_repository=catalog)

    response = client.get(
        _candidates_path(client), params={SEARCH_TERM_PARAM: new_opaque_token()}, headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    candidates = _candidates(response)
    assert candidates.catalog == []
    assert candidates.already_owned == already_owned
    assert candidates.store_consulted is False
    assert search.calls == []


def test_the_caller_can_still_reach_the_store_past_an_all_owned_answer():
    hit = _hit(new_game_title())
    catalog = FakeCatalogRepository(excluded_owned=new_positive_count())
    client, caller, _search = _build(FakeSearchClient(results=[hit]), catalog_repository=catalog)

    response = client.get(
        _candidates_path(client),
        params={SEARCH_TERM_PARAM: new_opaque_token(), INCLUDE_STORE_PARAM: True},
        headers=_bearer(caller.token),
    )

    assert response.status_code == 200
    assert _candidates(response).store == [_expected_store_row(hit, None)]


def test_an_empty_catalog_reaches_the_store_without_being_asked():
    expected_title = new_game_title()
    hit = _hit(expected_title)
    client, caller, search = _build(FakeSearchClient(results=[hit]))

    response = client.get(
        _candidates_path(client), params={SEARCH_TERM_PARAM: expected_title}, headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    candidates = _candidates(response)
    assert candidates.store == [_expected_store_row(hit, None)]
    assert candidates.store_consulted is True
    assert [query for query, _domain, _limit in search.calls] == [expected_title]


def test_the_store_is_reached_for_catalog_matches_the_caller_says_are_the_wrong_game():
    """The catalog is a partial mirror of the Store, so a search can return real titles and still miss the
    one in the caller's hands. Onimusha returned Warlords and Onimusha 2 while Way of the Sword was absent
    from the catalog entirely, and a client using "the catalog answered" as a proxy for "the catalog has
    this game" could never reach it."""
    searched_title = new_game_title()
    catalogued = _catalogued(new_game_title())
    hit = _hit(new_game_title())
    catalog = FakeCatalogRepository(catalog_games=[catalogued])
    client, caller, search = _build(FakeSearchClient(results=[hit]), catalog_repository=catalog)

    response = client.get(
        _candidates_path(client),
        params={SEARCH_TERM_PARAM: searched_title, INCLUDE_STORE_PARAM: True},
        headers=_bearer(caller.token),
    )

    assert response.status_code == 200
    candidates = _candidates(response)
    assert candidates.catalog == [to_game_summary_response(catalogued)]
    assert candidates.store == [_expected_store_row(hit, None)]
    assert candidates.store_consulted is True
    assert [query for query, _domain, _limit in search.calls] == [searched_title]


def test_a_store_candidate_the_catalog_already_holds_carries_its_game_id():
    hit = _hit(new_game_title())
    expected_game_id = new_game_id()
    catalog = FakeCatalogRepository(game_ids_by_store_id={hit.id: expected_game_id})
    client, caller, _search = _build(FakeSearchClient(results=[hit]), catalog_repository=catalog)

    response = client.get(
        _candidates_path(client), params={SEARCH_TERM_PARAM: new_opaque_token()}, headers=_bearer(caller.token)
    )

    assert _candidates(response).store == [_expected_store_row(hit, expected_game_id)]


def test_an_unlinked_caller_is_told_the_store_is_unavailable_rather_than_getting_an_error():
    """An unlinked account is a state of this answer, not a failure of the request -- the catalog half is
    still worth returning, and the panel has to say why the Store half is missing."""
    client, caller, _search = _build(linked=False)

    response = client.get(
        _candidates_path(client), params={SEARCH_TERM_PARAM: new_opaque_token()}, headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    candidates = _candidates(response)
    assert candidates.store_unavailable == STORE_UNAVAILABLE_NO_PSN_LINK
    assert candidates.store_consulted is False


def test_a_rejected_psn_token_is_reported_as_an_unavailable_store_not_a_401():
    client, caller, _search = _build(FakeSearchClient(raise_auth_error=True))

    response = client.get(
        _candidates_path(client), params={SEARCH_TERM_PARAM: new_opaque_token()}, headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    assert _candidates(response).store_unavailable == STORE_UNAVAILABLE_PSN_AUTH_FAILED


def test_one_limit_bounds_both_sources():
    requested_limit = new_small_count()
    catalog = FakeCatalogRepository()
    client, caller, search = _build(catalog_repository=catalog)

    response = client.get(
        _candidates_path(client),
        params={SEARCH_TERM_PARAM: new_opaque_token(), SEARCH_LIMIT_PARAM: requested_limit},
        headers=_bearer(caller.token),
    )

    assert response.status_code == 200
    assert [limit for _term, _owner, limit in catalog.searched] == [requested_limit]
    assert [limit for _query, _domain, limit in search.calls] == [requested_limit]


def test_the_candidates_route_will_not_spend_more_than_the_accept_route_can_find():
    client, caller, _search = _build()

    response = client.get(
        _candidates_path(client),
        params={SEARCH_TERM_PARAM: new_opaque_token(), SEARCH_LIMIT_PARAM: MAX_STORE_SEARCH_LIMIT + 1},
        headers=_bearer(caller.token),
    )

    assert response.status_code == 422


def test_requires_a_bearer_token():
    client, _caller, _search = _build()

    response = client.get(_search_path(client), params={SEARCH_TERM_PARAM: new_opaque_token()})

    assert response.status_code == 401


def test_an_unlinked_caller_gets_404_rather_than_an_empty_result_list():
    """An empty list would read as "the PS Store has never heard of this", which is a different and
    materially wrong answer from "we cannot ask on your behalf"."""
    client, caller, _search = _build(linked=False)

    response = client.get(
        _search_path(client), params={SEARCH_TERM_PARAM: new_opaque_token()}, headers=_bearer(caller.token)
    )

    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL


def test_returns_the_store_hits_for_a_linked_caller():
    expected_title = new_game_title()
    hit = _hit(expected_title)
    client, caller, _search = _build(FakeSearchClient(results=[hit]))

    response = client.get(
        _search_path(client), params={SEARCH_TERM_PARAM: expected_title}, headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    assert _search_results(response) == StoreSearchResponse(
        domain=FULL_GAMES_DOMAIN, results=[_expected_store_row(hit, None)]
    )


def test_passes_the_requested_domain_and_limit_through_to_psn():
    requested_limit = new_small_count()
    requested_query = new_opaque_token()
    search = FakeSearchClient()
    client, caller, _search = _build(search)

    response = client.get(
        _search_path(client),
        params={
            SEARCH_TERM_PARAM: requested_query,
            SEARCH_DOMAIN_PARAM: ADD_ONS_DOMAIN,
            SEARCH_LIMIT_PARAM: requested_limit,
        },
        headers=_bearer(caller.token),
    )

    assert response.status_code == 200
    assert search.calls == [(requested_query, ADD_ONS_DOMAIN, requested_limit)]
    assert _search_results(response).domain == ADD_ONS_DOMAIN
    assert [(row.action, row.detail, row.outcome) for row in client.app.state.audit_repository.rows] == [
        (ACTION_STORE_SEARCH, ADD_ONS_DOMAIN, OUTCOME_COMPLETED)
    ]


def test_a_store_search_is_not_sent_when_its_history_row_cannot_be_written():
    search = FakeSearchClient()
    client, caller, _search = _build(search)
    client.app.state.audit_repository.begin_error = RuntimeError(caller.sub)

    with pytest.raises(RuntimeError):
        client.get(_search_path(client), params={SEARCH_TERM_PARAM: new_opaque_token()}, headers=_bearer(caller.token))

    assert search.calls == []


def test_rejects_a_domain_outside_the_two_psn_publishes():
    client, caller, _search = _build()

    response = client.get(
        _search_path(client),
        params={SEARCH_TERM_PARAM: new_opaque_token(), SEARCH_DOMAIN_PARAM: lowercase_token()},
        headers=_bearer(caller.token),
    )

    assert response.status_code == 422


def test_rejects_a_blank_search_term():
    client, caller, _search = _build()

    response = client.get(_search_path(client), params={SEARCH_TERM_PARAM: ""}, headers=_bearer(caller.token))

    assert response.status_code == 422


def test_an_expired_psn_token_is_401_not_500():
    client, caller, _search = _build(FakeSearchClient(raise_auth_error=True))

    response = client.get(
        _search_path(client), params={SEARCH_TERM_PARAM: new_opaque_token()}, headers=_bearer(caller.token)
    )

    assert response.status_code == 401


def test_a_hit_the_catalog_already_holds_carries_its_game_id():
    hit = _hit(new_game_title())
    expected_game_id = new_game_id()
    catalog = FakeCatalogRepository(game_ids_by_store_id={hit.id: expected_game_id})
    client, caller, _search = _build(FakeSearchClient(results=[hit]), catalog_repository=catalog)

    response = client.get(
        _search_path(client), params={SEARCH_TERM_PARAM: new_opaque_token()}, headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    assert _search_results(response).results == [_expected_store_row(hit, expected_game_id)]
    assert catalog.looked_up == [[hit.id]]


def test_a_hit_the_catalog_has_never_seen_reports_no_game_id():
    hit = _hit(new_game_title())
    client, caller, _search = _build(FakeSearchClient(results=[hit]))

    response = client.get(
        _search_path(client), params={SEARCH_TERM_PARAM: new_opaque_token()}, headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    assert _search_results(response).results == [_expected_store_row(hit, None)]


def test_the_search_response_carries_the_concepts_default_product_id():
    expected_product_id = new_store_product_id(new_ps5_title_id())
    hit = _hit(new_game_title(), default_product_id=expected_product_id)
    client, caller, _search = _build(FakeSearchClient(results=[hit]))

    response = client.get(
        _search_path(client), params={SEARCH_TERM_PARAM: new_opaque_token()}, headers=_bearer(caller.token)
    )

    (result,) = _search_results(response).results
    assert result.default_product_id == expected_product_id


def test_accepting_a_store_hit_admits_it_and_adds_the_library_entry():
    expected_title = new_game_title()
    expected_concept_id = new_opaque_token()
    expected_product_id = new_store_product_id(new_ps5_title_id())
    expected_game_id = new_game_id()
    search_term = new_opaque_token()
    catalog = FakeCatalogRepository(admitted_game_id=expected_game_id)
    library = FakeLibraryRepository()
    client, caller, search = _build(
        FakeSearchClient(
            results=[
                _hit(
                    expected_title,
                    expected_concept_id,
                    platforms=(PS4, PS5),
                    default_product_id=expected_product_id,
                )
            ]
        ),
        catalog_repository=catalog,
        library_repository=library,
    )

    response = client.post(
        _manual_path(client),
        json=ManualGameRequest(store_hit=ManualStoreHitRequest(query=search_term, id=expected_concept_id)).model_dump(
            exclude_unset=True
        ),
        headers=_bearer(caller.token),
    )

    assert response.status_code == 204
    assert [row[:3] for row in catalog.admitted] == [(expected_concept_id, expected_title, expected_product_id)]
    assert library.manual_entries == [(caller.sub, expected_game_id, (PS4, PS5), None)]
    assert search.calls == [(search_term, FULL_GAMES_DOMAIN, MAX_STORE_SEARCH_LIMIT)]


def test_a_store_hit_the_catalog_already_resolves_takes_that_game_and_links_the_concept():
    """A hit resolved through a product id or a walked store_product_id would otherwise be admitted by
    name, and a store name that normalizes differently from the stored title forks the catalog."""
    concept_id = new_opaque_token()
    product_id = new_store_product_id(new_ps5_title_id())
    held_game_id = new_game_id()
    catalog = FakeCatalogRepository(game_ids_by_store_id={product_id: held_game_id})
    library = FakeLibraryRepository()
    hit = _hit(new_game_title(), concept_id, default_product_id=product_id)
    client, caller, _search = _build(
        FakeSearchClient(results=[hit]), catalog_repository=catalog, library_repository=library
    )

    response = client.post(_manual_path(client), json=_store_hit_body(concept_id), headers=_bearer(caller.token))

    assert response.status_code == 204
    assert catalog.admitted == [], "the catalog already holds this game; admitting again would fork it"
    assert catalog.linked == [(held_game_id, concept_id, product_id, hit.cover_image_url)]
    assert catalog.looked_up == [[concept_id, product_id]]
    assert library.manual_entries == [(caller.sub, held_game_id, (PS5,), None)]


def test_accepting_a_store_hit_never_trusts_the_clients_own_copy_of_the_title():
    """The catalog is shared across every user, so a body that could name its own canonical_title would
    let any linked caller write arbitrary rows into what everyone browses."""
    psn_title = new_game_title()
    client_claimed_title = new_game_title()
    concept_id = new_opaque_token()
    catalog = FakeCatalogRepository()
    client, caller, _search = _build(
        FakeSearchClient(results=[_hit(psn_title, concept_id)]), catalog_repository=catalog
    )

    client.post(
        _manual_path(client),
        json={**_store_hit_body(concept_id), "name": client_claimed_title, "canonical_title": client_claimed_title},
        headers=_bearer(caller.token),
    )

    assert [name for _concept, name, _product, _cover in catalog.admitted] == [psn_title]


def test_admitting_a_store_hit_keeps_the_cover_the_search_returned():
    """The image is otherwise unrecoverable: psn_catalog_cache is keyed on an npTitleId a store search
    never returns, so nothing running later can fetch what admission discarded. PSN carries no entitlement
    artwork for much of the PS3/Vita/PSP back catalogue, which is exactly where a hand-added disc lands."""
    concept_id = new_opaque_token()
    hit = _hit(new_game_title(), concept_id)
    catalog = FakeCatalogRepository()
    client, caller, _search = _build(FakeSearchClient(results=[hit]), catalog_repository=catalog)

    response = client.post(_manual_path(client), json=_store_hit_body(concept_id), headers=_bearer(caller.token))

    assert response.status_code == 204
    assert [cover for _concept, _name, _product, cover in catalog.admitted] == [hit.cover_image_url]


def test_a_store_hit_id_psn_did_not_return_is_404_and_admits_nothing():
    catalog = FakeCatalogRepository()
    client, caller, _search = _build(FakeSearchClient(results=[_hit(new_game_title())]), catalog_repository=catalog)

    response = client.post(
        _manual_path(client), json=_store_hit_body(new_opaque_token()), headers=_bearer(caller.token)
    )

    assert response.status_code == 404
    assert catalog.admitted == []


def test_accepting_a_store_hit_searches_only_the_full_games_domain():
    """A manual library entry records a game the user owns; the add-ons domain answers with cash cards."""
    concept_id = new_opaque_token()
    client, caller, search = _build(FakeSearchClient(results=[_hit(new_game_title(), concept_id)]))

    client.post(
        _manual_path(client),
        json={**_store_hit_body(concept_id), SEARCH_DOMAIN_PARAM: ADD_ONS_DOMAIN},
        headers=_bearer(caller.token),
    )

    assert [domain for _query, domain, _limit in search.calls] == [FULL_GAMES_DOMAIN]


def test_a_platform_psn_publishes_that_curator_has_no_vocabulary_for_is_dropped_not_fatal():
    concept_id = new_opaque_token()
    unknown_platform = lowercase_token().upper()
    library = FakeLibraryRepository()
    client, caller, _search = _build(
        FakeSearchClient(results=[_hit(new_game_title(), concept_id, platforms=(unknown_platform, PS5))]),
        library_repository=library,
    )

    response = client.post(_manual_path(client), json=_store_hit_body(concept_id), headers=_bearer(caller.token))

    assert response.status_code == 204
    assert library.manual_entries[0][2] == (PS5,)


def test_a_caller_named_platform_wins_over_the_one_psn_published():
    concept_id = new_opaque_token()
    library = FakeLibraryRepository()
    client, caller, _search = _build(
        FakeSearchClient(results=[_hit(new_game_title(), concept_id, platforms=(PS5,))]),
        library_repository=library,
    )

    client.post(_manual_path(client), json=_store_hit_body(concept_id, [PS3]), headers=_bearer(caller.token))

    assert library.manual_entries[0][2] == (PS3,)


def test_the_psn_page_cap_can_still_reach_the_largest_limit_this_route_offers():
    """Lives here rather than beside the cap because it is a statement about the route's promise, not
    about the client. The cap's own docstring derives 3 from these two numbers; a docstring cannot fail
    when someone lowers the cap, and the symptom would be a hit the search route showed that the accept
    route then reported as not in the store."""
    smallest_first_page_psn_has_returned = 15

    reachable = (MAX_GAME_SEARCH_PAGES + 1) * smallest_first_page_psn_has_returned

    assert reachable >= MAX_STORE_SEARCH_LIMIT


def test_a_bad_platform_on_a_store_hit_is_rejected_before_anything_is_admitted():
    """The ``game_id`` branch checks the resource before the platform vocabulary because a 404 there
    outranks a 400. This branch has no such resource to check first, and admitting a game to the shared
    catalog and only then rejecting the request would leave a write behind a failed call."""
    concept_id = new_opaque_token()
    catalog = FakeCatalogRepository()
    client, caller, search = _build(
        FakeSearchClient(results=[_hit(new_game_title(), concept_id)]), catalog_repository=catalog
    )

    response = client.post(
        _manual_path(client), json=_store_hit_body(concept_id, [lowercase_token()]), headers=_bearer(caller.token)
    )

    assert response.status_code == 400
    assert catalog.admitted == []
    assert search.calls == []


def test_naming_both_a_game_id_and_a_store_hit_is_rejected():
    client, caller, _search = _build()
    body = ManualGameRequest.model_construct(
        game_id=new_game_id(), store_hit=ManualStoreHitRequest(query=new_opaque_token(), id=new_opaque_token())
    ).model_dump(exclude_unset=True)

    response = client.post(_manual_path(client), json=body, headers=_bearer(caller.token))

    assert response.status_code == 422


def test_naming_neither_a_game_id_nor_a_store_hit_is_rejected():
    client, caller, _search = _build()
    body = ManualGameRequest.model_construct(platforms=[PS5]).model_dump(exclude_unset=True)

    response = client.post(_manual_path(client), json=body, headers=_bearer(caller.token))

    assert response.status_code == 422


def test_an_unlinked_caller_cannot_accept_a_store_hit():
    client, caller, _search = _build(linked=False)

    response = client.post(
        _manual_path(client), json=_store_hit_body(new_opaque_token()), headers=_bearer(caller.token)
    )

    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL


def test_no_harvest_preference_gates_the_store_lookup():
    """The link seeded here has every harvest_* flag at its 0002 default of false. A store search reads
    the public catalog, never the caller's own PSN data, so gating it on one would leave the feature dark
    behind a toggle the user never saw."""
    expected_title = new_game_title()
    hit = _hit(expected_title)
    client, caller, _search = _build(FakeSearchClient(results=[hit]))

    response = client.get(
        _search_path(client), params={SEARCH_TERM_PARAM: expected_title}, headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    assert _search_results(response).results == [_expected_store_row(hit, None)]
