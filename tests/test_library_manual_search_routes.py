"""Tests for GET /library/manual/search -- create_app wired with a hand-written fake
social_client_factory, mirroring test_devices_routes.py's style.
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from curator.app import create_app
from curator.library_routes import MAX_STORE_SEARCH_LIMIT
from curator.persistence.crypto import TokenCrypto
from curator.psn.errors import PsnAuthError
from curator.psn.models import GameSearchResult
from curator.psn.social_client import ADD_ONS_DOMAIN, FULL_GAMES_DOMAIN, MAX_GAME_SEARCH_PAGES
from test_routes import SUB, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _seed_link

_SEARCH_URL = "/library/manual/search"


class FakeSearchClient:
    """Stands in for SocialClient: records every search, returns canned hits, or raises when armed."""

    def __init__(self, *, results=(), raise_auth_error=False):
        self.results = list(results)
        self.raise_auth_error = raise_auth_error
        self.calls: list[tuple[str, str, int]] = []

    async def universal_search_games(self, query, *, domain=FULL_GAMES_DOMAIN, limit=20):
        self.calls.append((query, domain, limit))
        if self.raise_auth_error:
            raise PsnAuthError("boom")
        return self.results[:limit]


class FakeSearchClientFactory:
    """Raises ``RuntimeError`` for any ``sub`` not explicitly linked, as the real factory does."""

    def __init__(self):
        self.linked: dict[str, FakeSearchClient] = {}

    async def __call__(self, sub):
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(f"No PSN link for user {sub!r}; cannot build a social client.")
        return client


class FakeCatalogRepository:
    """Records every admission and answers the catalog-membership lookup from a fixed mapping."""

    def __init__(self, game_ids_by_store_id=None, admitted_game_id=None):
        self._game_ids_by_store_id = dict(game_ids_by_store_id or {})
        self._admitted_game_id = admitted_game_id or str(uuid.uuid4())
        self.admitted: list[tuple[str, str, str | None]] = []
        self.looked_up: list[list[str]] = []

    async def game_ids_for_store_ids(self, store_ids):
        self.looked_up.append(list(store_ids))
        return {
            store_id: self._game_ids_by_store_id[store_id]
            for store_id in store_ids
            if store_id in self._game_ids_by_store_id
        }

    async def admit_store_game(self, *, concept_id, name, product_id=None):
        self.admitted.append((concept_id, name, product_id))
        return self._admitted_game_id, True

    async def game_exists(self, game_id):
        return game_id in self._game_ids_by_store_id.values()

    async def title_id_for_game(self, game_id):
        return None


class FakeLibraryRepository:
    """Records manual upserts so a route test can assert what reached the library."""

    def __init__(self):
        self.manual_entries: list[tuple[str, str, tuple[str, ...], str | None]] = []

    async def upsert_manual_entry(self, identity_sub, game_id, *, platforms, owned_edition):
        self.manual_entries.append((identity_sub, game_id, tuple(platforms), owned_edition))


def _build(search_client=None, *, linked=True, catalog_repository=None, library_repository=None):
    validator = FakeTokenValidator()
    factory = FakeSearchClientFactory()
    client = search_client if search_client is not None else FakeSearchClient()
    repository = FakeRepository()
    if linked:
        factory.linked[SUB] = client
        _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), SUB)
    app = create_app(
        _make_settings(),
        repository=repository,
        token_validator=validator,
        social_client_factory=factory,
    )
    app.state.catalog_repository = catalog_repository or FakeCatalogRepository()
    app.state.library_repository = library_repository or FakeLibraryRepository()
    return TestClient(app), validator, client


def _authorized(validator):
    token = uuid.uuid4().hex
    validator.register(token, _claims(sub=SUB))
    return _bearer(token)


def _hit(name, store_id=None, *, kind="Concept", platforms=("PS5",), default_product_id=None):
    return GameSearchResult(
        id=store_id or uuid.uuid4().hex,
        kind=kind,
        default_product_id=default_product_id,
        name=name,
        platforms=platforms,
        cover_image_url=f"https://cdn.invalid/{uuid.uuid4().hex}.jpg",
        classification="Full Game",
        price=None,
        discounted_price=None,
        is_free=None,
    )


def test_requires_a_bearer_token():
    client, _validator, _search = _build()

    response = client.get(_SEARCH_URL, params={"q": uuid.uuid4().hex})

    assert response.status_code == 401


def test_an_unlinked_caller_gets_404_rather_than_an_empty_result_list():
    """An empty list would read as "the PS Store has never heard of this", which is a different and
    materially wrong answer from "we cannot ask on your behalf"."""
    client, validator, _search = _build(linked=False)

    response = client.get(_SEARCH_URL, params={"q": uuid.uuid4().hex}, headers=_authorized(validator))

    assert response.status_code == 404


def test_returns_the_store_hits_for_a_linked_caller():
    expected_title = f"Ghost of {uuid.uuid4().hex}"
    expected_store_id = uuid.uuid4().hex
    client, validator, _search = _build(FakeSearchClient(results=[_hit(expected_title, expected_store_id)]))

    response = client.get(_SEARCH_URL, params={"q": expected_title}, headers=_authorized(validator))

    assert response.status_code == 200
    body = response.json()
    assert body["domain"] == FULL_GAMES_DOMAIN
    assert [(row["id"], row["name"], row["kind"]) for row in body["results"]] == [
        (expected_store_id, expected_title, "Concept")
    ]


def test_passes_the_requested_domain_and_limit_through_to_psn():
    requested_limit = 3
    requested_query = uuid.uuid4().hex
    search = FakeSearchClient()
    client, validator, _search = _build(search)

    response = client.get(
        _SEARCH_URL,
        params={"q": requested_query, "domain": ADD_ONS_DOMAIN, "limit": requested_limit},
        headers=_authorized(validator),
    )

    assert response.status_code == 200
    assert search.calls == [(requested_query, ADD_ONS_DOMAIN, requested_limit)]
    assert response.json()["domain"] == ADD_ONS_DOMAIN


def test_rejects_a_domain_outside_the_two_psn_publishes():
    client, validator, _search = _build()

    response = client.get(
        _SEARCH_URL,
        params={"q": uuid.uuid4().hex, "domain": f"Mobile{uuid.uuid4().hex}"},
        headers=_authorized(validator),
    )

    assert response.status_code == 422


def test_rejects_a_blank_search_term():
    client, validator, _search = _build()

    response = client.get(_SEARCH_URL, params={"q": ""}, headers=_authorized(validator))

    assert response.status_code == 422


def test_an_expired_psn_token_is_401_not_500():
    client, validator, _search = _build(FakeSearchClient(raise_auth_error=True))

    response = client.get(_SEARCH_URL, params={"q": uuid.uuid4().hex}, headers=_authorized(validator))

    assert response.status_code == 401


def test_a_hit_the_catalog_already_holds_carries_its_game_id():
    expected_store_id = uuid.uuid4().hex
    expected_game_id = str(uuid.uuid4())
    catalog = FakeCatalogRepository(game_ids_by_store_id={expected_store_id: expected_game_id})
    client, validator, _search = _build(
        FakeSearchClient(results=[_hit(f"Ghost of {uuid.uuid4().hex}", expected_store_id)]),
        catalog_repository=catalog,
    )

    response = client.get(_SEARCH_URL, params={"q": uuid.uuid4().hex}, headers=_authorized(validator))

    assert response.status_code == 200
    assert [row["game_id"] for row in response.json()["results"]] == [expected_game_id]
    assert catalog.looked_up == [[expected_store_id]]


def test_a_hit_the_catalog_has_never_seen_reports_no_game_id():
    client, validator, _search = _build(FakeSearchClient(results=[_hit(f"Ghost of {uuid.uuid4().hex}")]))

    response = client.get(_SEARCH_URL, params={"q": uuid.uuid4().hex}, headers=_authorized(validator))

    assert response.status_code == 200
    assert response.json()["results"][0]["game_id"] is None


def test_the_search_response_carries_the_concepts_default_product_id():
    expected_product_id = "UP1004-PPSA03420_00-GTAOSTANDALONE01"
    client, validator, _search = _build(
        FakeSearchClient(results=[_hit(f"Ghost of {uuid.uuid4().hex}", default_product_id=expected_product_id)])
    )

    response = client.get(_SEARCH_URL, params={"q": uuid.uuid4().hex}, headers=_authorized(validator))

    assert response.json()["results"][0]["default_product_id"] == expected_product_id


def test_accepting_a_store_hit_admits_it_and_adds_the_library_entry():
    expected_title = f"Ghost of {uuid.uuid4().hex}"
    expected_concept_id = uuid.uuid4().hex
    expected_product_id = "UP1004-PPSA03420_00-GTAOSTANDALONE01"
    expected_game_id = str(uuid.uuid4())
    search_term = uuid.uuid4().hex
    catalog = FakeCatalogRepository(admitted_game_id=expected_game_id)
    library = FakeLibraryRepository()
    client, validator, search = _build(
        FakeSearchClient(
            results=[
                _hit(
                    expected_title,
                    expected_concept_id,
                    platforms=("PS4", "PS5"),
                    default_product_id=expected_product_id,
                )
            ]
        ),
        catalog_repository=catalog,
        library_repository=library,
    )

    response = client.post(
        "/library/manual",
        json={"store_hit": {"query": search_term, "id": expected_concept_id}},
        headers=_authorized(validator),
    )

    assert response.status_code == 204
    assert catalog.admitted == [(expected_concept_id, expected_title, expected_product_id)]
    assert library.manual_entries == [(SUB, expected_game_id, ("PS4", "PS5"), None)]
    assert search.calls == [(search_term, FULL_GAMES_DOMAIN, MAX_STORE_SEARCH_LIMIT)]


def test_accepting_a_store_hit_never_trusts_the_clients_own_copy_of_the_title():
    """The catalog is shared across every user, so a body that could name its own canonical_title would
    let any linked caller write arbitrary rows into what everyone browses."""
    psn_title = f"Ghost of {uuid.uuid4().hex}"
    concept_id = uuid.uuid4().hex
    catalog = FakeCatalogRepository()
    client, validator, _search = _build(
        FakeSearchClient(results=[_hit(psn_title, concept_id)]), catalog_repository=catalog
    )

    client.post(
        "/library/manual",
        json={
            "store_hit": {"query": uuid.uuid4().hex, "id": concept_id},
            "name": "Totally Not What PSN Said",
            "canonical_title": "Totally Not What PSN Said",
        },
        headers=_authorized(validator),
    )

    assert [name for _concept, name, _product in catalog.admitted] == [psn_title]


def test_a_store_hit_id_psn_did_not_return_is_404_and_admits_nothing():
    catalog = FakeCatalogRepository()
    client, validator, _search = _build(
        FakeSearchClient(results=[_hit(f"Ghost of {uuid.uuid4().hex}")]), catalog_repository=catalog
    )

    response = client.post(
        "/library/manual",
        json={"store_hit": {"query": uuid.uuid4().hex, "id": uuid.uuid4().hex}},
        headers=_authorized(validator),
    )

    assert response.status_code == 404
    assert catalog.admitted == []


def test_accepting_a_store_hit_searches_only_the_full_games_domain():
    """A manual library entry records a game the user owns; the add-ons domain answers with cash cards."""
    concept_id = uuid.uuid4().hex
    client, validator, search = _build(FakeSearchClient(results=[_hit(f"Ghost of {uuid.uuid4().hex}", concept_id)]))

    client.post(
        "/library/manual",
        json={"store_hit": {"query": uuid.uuid4().hex, "id": concept_id}, "domain": ADD_ONS_DOMAIN},
        headers=_authorized(validator),
    )

    assert [domain for _query, domain, _limit in search.calls] == [FULL_GAMES_DOMAIN]


def test_a_platform_psn_publishes_that_curator_has_no_vocabulary_for_is_dropped_not_fatal():
    concept_id = uuid.uuid4().hex
    library = FakeLibraryRepository()
    client, validator, _search = _build(
        FakeSearchClient(results=[_hit(f"Ghost of {uuid.uuid4().hex}", concept_id, platforms=("PS6", "PS5"))]),
        library_repository=library,
    )

    response = client.post(
        "/library/manual",
        json={"store_hit": {"query": uuid.uuid4().hex, "id": concept_id}},
        headers=_authorized(validator),
    )

    assert response.status_code == 204
    assert library.manual_entries[0][2] == ("PS5",)


def test_a_caller_named_platform_wins_over_the_one_psn_published():
    concept_id = uuid.uuid4().hex
    library = FakeLibraryRepository()
    client, validator, _search = _build(
        FakeSearchClient(results=[_hit(f"Ghost of {uuid.uuid4().hex}", concept_id, platforms=("PS5",))]),
        library_repository=library,
    )

    client.post(
        "/library/manual",
        json={"store_hit": {"query": uuid.uuid4().hex, "id": concept_id}, "platforms": ["PS3"]},
        headers=_authorized(validator),
    )

    assert library.manual_entries[0][2] == ("PS3",)


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
    concept_id = uuid.uuid4().hex
    catalog = FakeCatalogRepository()
    client, validator, search = _build(
        FakeSearchClient(results=[_hit(f"Ghost of {uuid.uuid4().hex}", concept_id)]), catalog_repository=catalog
    )

    response = client.post(
        "/library/manual",
        json={"store_hit": {"query": uuid.uuid4().hex, "id": concept_id}, "platforms": ["Xbox"]},
        headers=_authorized(validator),
    )

    assert response.status_code == 400
    assert catalog.admitted == []
    assert search.calls == []


def test_naming_both_a_game_id_and_a_store_hit_is_rejected():
    client, validator, _search = _build()

    response = client.post(
        "/library/manual",
        json={"game_id": str(uuid.uuid4()), "store_hit": {"query": uuid.uuid4().hex, "id": uuid.uuid4().hex}},
        headers=_authorized(validator),
    )

    assert response.status_code == 422


def test_naming_neither_a_game_id_nor_a_store_hit_is_rejected():
    client, validator, _search = _build()

    response = client.post("/library/manual", json={"platforms": ["PS5"]}, headers=_authorized(validator))

    assert response.status_code == 422


def test_an_unlinked_caller_cannot_accept_a_store_hit():
    client, validator, _search = _build(linked=False)

    response = client.post(
        "/library/manual",
        json={"store_hit": {"query": uuid.uuid4().hex, "id": uuid.uuid4().hex}},
        headers=_authorized(validator),
    )

    assert response.status_code == 404


def test_no_harvest_preference_gates_the_store_lookup():
    """The link seeded here has every harvest_* flag at its 0002 default of false. A store search reads
    the public catalog, never the caller's own PSN data, so gating it on one would leave the feature dark
    behind a toggle the user never saw."""
    expected_title = f"Ghost of {uuid.uuid4().hex}"
    client, validator, _search = _build(FakeSearchClient(results=[_hit(expected_title)]))

    response = client.get(_SEARCH_URL, params={"q": expected_title}, headers=_authorized(validator))

    assert response.status_code == 200
    assert [row["name"] for row in response.json()["results"]] == [expected_title]
