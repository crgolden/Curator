"""Tests for POST /collections/preview, using create_app() with fake CatalogRepository/CollectionOrchestrator."""

from __future__ import annotations

from dataclasses import replace

import psycopg
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.collections.collection_orchestrator import CollectionResult
from curator.collections.filter_predicate import And, GenreIn, Or, ScoreAtLeast, TierIn
from curator.collections.game_candidate import GameCandidate
from curator.collections.repository import CollectionDefinition, CollectionItem, UserConsole
from curator.persistence.crypto import TokenCrypto
from test_routes import (
    FakeAgentFactory,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _seed_link,
)
from test_trophy_routes import FakeTrophyClient, FakeTrophyClientFactory


class FakeCatalogRepository:
    async def get_size_estimates(self):
        return []


class FakeOrchestrator:
    def __init__(self, result=None, raises=None):
        self._result = result or CollectionResult(included=(), excluded=(), used_gb=None)
        self._raises = raises
        self.generate_calls = []

    async def generate(self, identity_sub, spec, *, size_estimates, completion_map=None, completion_available=False):
        self.generate_calls.append((identity_sub, spec, completion_map, completion_available))
        if self._raises:
            raise self._raises
        return self._result


class FakeCollectionsRepository:
    def __init__(
        self, definitions=None, consoles=None, duplicate_names=(), known_games=(), malformed_game_ids=(), candidates=()
    ):
        self.definitions: dict[str, CollectionDefinition] = {d.definition_id: d for d in (definitions or [])}
        self.items: dict[str, tuple[str, ...]] = {}
        self.saved_runs: list[tuple] = []
        self.consoles: list[UserConsole] = list(consoles or [])
        self.duplicate_names = set(duplicate_names)
        self.known_games = set(known_games)
        self.malformed_game_ids = set(malformed_game_ids)
        self._candidates = list(candidates)
        self._next_id = 1
        self.collection_follows: dict[str, set[str]] = {}

    async def list_user_consoles(self, identity_sub):
        return self.consoles

    async def list_candidates(self, identity_sub, *, platform=None, include_inactive=False, min_percent_completed=None):
        return self._candidates

    async def existing_game_ids(self, game_ids):
        if self.malformed_game_ids & set(game_ids):
            raise psycopg.errors.InvalidTextRepresentation("invalid input syntax for type uuid")
        return {game_id for game_id in game_ids if game_id in self.known_games}

    async def save_definition(self, identity_sub, name, spec, *, description=None, game_ids=()):
        if name in self.duplicate_names:
            raise psycopg.errors.UniqueViolation(
                'duplicate key value violates unique constraint "collection_definitions_identity_sub_name_key"'
            )
        definition_id = f"def-{self._next_id}"
        self._next_id += 1
        self.definitions[definition_id] = CollectionDefinition(
            definition_id=definition_id,
            identity_sub=identity_sub,
            name=name,
            kind=spec.kind,
            console_id=spec.console_id,
            genre_filter=spec.genre_filter,
            min_score=spec.min_score,
            aaa_tier_filter=spec.aaa_tier_filter,
            sort_order=spec.sort_order,
            description=description,
            min_percent_completed=spec.min_percent_completed,
            filter_predicate=spec.filter_predicate,
            share_slug=f"slug-{definition_id}",
            exclude_installed_on=spec.exclude_installed_on,
        )
        self.items[definition_id] = tuple(game_ids)
        return definition_id

    def _with_live_item_count(self, definition):
        return replace(definition, item_count=len(self.items.get(definition.definition_id, ())))

    async def list_definitions(self, identity_sub):
        return [self._with_live_item_count(d) for d in self.definitions.values() if d.identity_sub == identity_sub]

    async def get_definition(self, identity_sub, definition_id):
        definition = self.definitions.get(definition_id)
        if definition is None or definition.identity_sub != identity_sub:
            return None
        return self._with_live_item_count(definition)

    async def get_definition_any_owner(self, definition_id):
        definition = self.definitions.get(definition_id)
        return None if definition is None else self._with_live_item_count(definition)

    async def get_definition_by_share_slug(self, share_slug):
        for definition in self.definitions.values():
            if definition.share_slug == share_slug and definition.visibility != "private":
                return self._with_live_item_count(definition)
        return None

    async def set_definition_visibility(self, identity_sub, definition_id, visibility):
        definition = self.definitions.get(definition_id)
        if definition is None or definition.identity_sub != identity_sub:
            return None
        self.definitions[definition_id] = replace(definition, visibility=visibility)
        return self._with_live_item_count(self.definitions[definition_id])

    async def follow_collection(self, follower_sub, definition_id):
        self.collection_follows.setdefault(definition_id, set()).add(follower_sub)

    async def unfollow_collection(self, follower_sub, definition_id):
        followers = self.collection_follows.get(definition_id, set())
        if follower_sub in followers:
            followers.remove(follower_sub)
            return True
        return False

    async def list_followed_collections(self, follower_sub):
        return [
            self._with_live_item_count(definition)
            for definition_id, definition in self.definitions.items()
            if follower_sub in self.collection_follows.get(definition_id, set())
        ]

    async def list_definition_items(self, definition_id):
        return [
            CollectionItem(
                game_id=game_id,
                rank=rank,
                title=f"Game {game_id}",
                franchise=None,
                genre=None,
                aaa_tier=None,
                critical_score=None,
                oc_score=None,
                psn_rating=None,
                cover_image_url=f"{game_id}.png",
                owner_has_access=True,
            )
            for rank, game_id in enumerate(self.items.get(definition_id, ()), start=1)
        ]

    async def list_definition_items_page(
        self, definition_id, *, search=None, genre=None, sort="rank", sort_dir="asc", limit=50, offset=0
    ):
        items = await self.list_definition_items(definition_id)
        if search:
            items = [item for item in items if search.lower() in item.title.lower()]
        return items[offset : offset + limit], len(items)

    async def remove_definition_item(self, definition_id, game_id):
        members = self.items.get(definition_id, ())
        if game_id not in members:
            return False
        self.items[definition_id] = tuple(member for member in members if member != game_id)
        return True

    async def update_definition(self, definition_id, *, name, description, game_ids=None):
        if name in self.duplicate_names:
            raise psycopg.errors.UniqueViolation(
                'duplicate key value violates unique constraint "collection_definitions_identity_sub_name_key"'
            )
        existing = self.definitions[definition_id]
        self.definitions[definition_id] = replace(existing, name=name, description=description)
        if game_ids is not None:
            self.items[definition_id] = tuple(game_ids)

    async def delete_definition(self, identity_sub, definition_id):
        definition = self.definitions.get(definition_id)
        if definition is None or definition.identity_sub != identity_sub:
            return False
        del self.definitions[definition_id]
        self.items.pop(definition_id, None)
        return True

    async def save_run(self, identity_sub, definition_id, spec_snapshot, included, excluded):
        self.saved_runs.append((identity_sub, definition_id, spec_snapshot, included, excluded))
        return "run-1"


def _build(orchestrator=None, collections_repository=None, repository=None, trophy_client_factory=None):
    repository = repository if repository is not None else FakeRepository()
    token_crypto = TokenCrypto(TokenCrypto.generate_key())
    validator = FakeTokenValidator()
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=validator,
        catalog_repository=FakeCatalogRepository(),
        trophy_client_factory=trophy_client_factory,
    )
    app.state.collection_orchestrator = orchestrator or FakeOrchestrator()
    app.state.collections_repository = collections_repository or FakeCollectionsRepository()
    return TestClient(app), validator


def test_requires_bearer_token():
    client, _validator = _build()

    response = client.post("/collections/preview", json={"kind": "filter_list"})

    assert response.status_code == 401


def test_invalid_kind_is_rejected():
    client, validator = _build()
    validator.register("token-a", _claims())

    response = client.post("/collections/preview", json={"kind": "bogus"}, headers=_bearer("token-a"))

    assert response.status_code == 400


def test_orchestrator_value_error_becomes_400():
    orchestrator = FakeOrchestrator(raises=ValueError("Unknown console_id"))
    client, validator = _build(orchestrator)
    validator.register("token-a", _claims())

    response = client.post(
        "/collections/preview",
        json={"kind": "capacity_fill", "console_id": "missing"},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400
    assert "Unknown console_id" in response.json()["detail"]


def test_returns_generated_candidates():
    candidate = GameCandidate(
        game_id="g1",
        title="God of War",
        genre="Action",
        aaa_tier="AAA",
        franchise="God of War",
        composite_score=90.0,
        rank_score=3,
        size_gb=50.0,
    )
    result = CollectionResult(included=(candidate,), excluded=(), used_gb=50.0)
    orchestrator = FakeOrchestrator(result=result)
    client, validator = _build(orchestrator)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/collections/preview", json={"kind": "filter_list"}, headers=_bearer("token-a"))

    assert response.status_code == 200
    body = response.json()
    assert body["used_gb"] == 50.0
    assert body["included"][0]["game_id"] == "g1"
    assert orchestrator.generate_calls[0][0] == "sub-a"
    assert orchestrator.generate_calls[0][1].kind == "filter_list"


def test_preview_passes_min_percent_completed_through_to_spec():
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator)
    validator.register("token-a", _claims(sub="sub-a"))

    client.post(
        "/collections/preview",
        json={"kind": "filter_list", "min_percent_completed": 50},
        headers=_bearer("token-a"),
    )

    assert orchestrator.generate_calls[0][1].min_percent_completed == 50


def test_preview_passes_sort_order_and_exclude_installed_on_through_to_spec():
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator)
    validator.register("token-a", _claims(sub="sub-a"))

    client.post(
        "/collections/preview",
        json={"kind": "filter_list", "sort_order": "composite_desc", "exclude_installed_on": ["c1", "c2"]},
        headers=_bearer("token-a"),
    )

    spec = orchestrator.generate_calls[0][1]
    assert spec.sort_order == "composite_desc"
    assert spec.exclude_installed_on == ("c1", "c2")


def test_preview_response_includes_percent_completed():
    candidate = GameCandidate(
        game_id="g1",
        title="God of War",
        genre="Action",
        aaa_tier="AAA",
        franchise="God of War",
        composite_score=90.0,
        rank_score=3,
        size_gb=50.0,
        percent_completed=87,
    )
    result = CollectionResult(included=(candidate,), excluded=(), used_gb=None)
    client, validator = _build(FakeOrchestrator(result=result))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/collections/preview", json={"kind": "filter_list"}, headers=_bearer("token-a"))

    assert response.json()["included"][0]["percent_completed"] == 87


def test_preview_never_resolves_trophy_completion_at_request_time():
    """A preview must not touch PSN. Completion is read from each candidate row.

    Before ``0015_library_entries_trophy_progress.sql`` this route fetched the caller's whole library and
    fuzzy-matched every game against their trophy titles on every request -- measured at ~38s for a
    411-game library, on the event loop. The percentage is now persisted, so the route hands the
    orchestrator nothing and the row's own value stands.
    """
    repository = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    _seed_link(repository, crypto, "sub-a", harvest_trophies=True)
    factory = FakeTrophyClientFactory()
    factory.linked["sub-a"] = FakeTrophyClient(titles=[])
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator, repository=repository, trophy_client_factory=factory)
    validator.register("token-a", _claims(sub="sub-a"))

    client.post("/collections/preview", json={"kind": "filter_list"}, headers=_bearer("token-a"))

    _, _, completion_map, completion_available = orchestrator.generate_calls[0]
    assert completion_map is None
    assert completion_available is False
    assert factory.calls == []


def test_preview_threads_the_completion_floor_through_to_the_spec():
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator)
    validator.register("token-a", _claims(sub="sub-a"))

    client.post(
        "/collections/preview",
        json={"kind": "filter_list", "min_percent_completed": 80},
        headers=_bearer("token-a"),
    )

    _, spec, _, _ = orchestrator.generate_calls[0]
    assert spec.min_percent_completed == 80


def test_preview_parses_a_filter_predicate_onto_the_spec():
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/collections/preview",
        json={
            "kind": "filter_list",
            "filter_predicate": {
                "op": "or",
                "nodes": [
                    {"op": "genre_in", "values": ["RPG"]},
                    {
                        "op": "and",
                        "nodes": [{"op": "genre_in", "values": ["Action"]}, {"op": "tier_in", "values": ["Indie"]}],
                    },
                ],
            },
        },
        headers=_bearer("token-a"),
    )

    assert response.status_code == 200
    spec = orchestrator.generate_calls[0][1]
    assert spec.filter_predicate == Or(
        nodes=(GenreIn(values=("RPG",)), And(nodes=(GenreIn(values=("Action",)), TierIn(values=("Indie",)))))
    )


def test_preview_rejects_a_malformed_filter_predicate_as_400_not_500():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/collections/preview",
        json={"kind": "filter_list", "filter_predicate": {"op": "bogus"}},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400


def test_save_definition_rejects_invalid_kind():
    client, validator = _build()
    validator.register("token-a", _claims())

    response = client.post("/collections", json={"name": "x", "kind": "bogus"}, headers=_bearer("token-a"))

    assert response.status_code == 400


def test_save_definition_persists_and_returns_it():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/collections",
        json={"name": "My RPGs", "kind": "filter_list", "genre_filter": ["RPG"], "min_score": 80.0},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "My RPGs"
    assert body["genre_filter"] == ["RPG"]
    assert len(collections_repository.definitions) == 1


def test_save_definition_rejects_a_console_the_caller_does_not_own():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/collections",
        json={"name": "Someone else's PS5", "kind": "capacity_fill", "console_id": "console-not-mine"},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400
    assert "Unknown console_id" in response.json()["detail"]
    assert collections_repository.definitions == {}


def test_save_definition_rejects_an_exclude_installed_on_console_the_caller_does_not_own():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/collections",
        json={"name": "Not on my Vita", "kind": "filter_list", "exclude_installed_on": ["console-not-mine"]},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400
    assert "exclude_installed_on" in response.json()["detail"]
    assert collections_repository.definitions == {}


def test_save_definition_round_trips_sort_order_and_exclude_installed_on():
    console = UserConsole(
        console_id="console-a",
        name="Living room PS5",
        platform="PS5",
        raw_capacity_gb=800.0,
        update_buffer_gb=50.0,
        routing_genres=(),
        fill_order=0,
    )
    collections_repository = FakeCollectionsRepository(consoles=[console])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/collections",
        json={
            "name": "Not on my PS5",
            "kind": "filter_list",
            "sort_order": "composite_desc",
            "exclude_installed_on": ["console-a"],
        },
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    body = response.json()
    assert body["sort_order"] == "composite_desc"
    assert body["exclude_installed_on"] == ["console-a"]


def test_save_definition_accepts_a_console_the_caller_owns():
    console = UserConsole(
        console_id="console-a",
        name="Living room PS5",
        platform="PS5",
        raw_capacity_gb=800.0,
        update_buffer_gb=50.0,
        routing_genres=(),
        fill_order=0,
    )
    collections_repository = FakeCollectionsRepository(consoles=[console])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/collections",
        json={"name": "PS5 fill", "kind": "capacity_fill", "console_id": "console-a"},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    assert response.json()["console_id"] == "console-a"


def test_save_definition_persists_min_percent_completed():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/collections",
        json={"name": "Nearly Done", "kind": "filter_list", "min_percent_completed": 75},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    assert response.json()["min_percent_completed"] == 75
    assert collections_repository.definitions["def-1"].min_percent_completed == 75


def test_save_definition_duplicate_name_returns_409():
    collections_repository = FakeCollectionsRepository(duplicate_names={"My RPGs"})
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/collections",
        json={"name": "My RPGs", "kind": "filter_list"},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 409
    assert "My RPGs" in response.json()["detail"]


def test_list_definitions_scopes_to_caller():
    definition_a = CollectionDefinition(
        definition_id="def-a",
        identity_sub="sub-a",
        name="A's list",
        kind="filter_list",
        console_id=None,
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
    )
    definition_b = CollectionDefinition(
        definition_id="def-b",
        identity_sub="sub-b",
        name="B's list",
        kind="filter_list",
        console_id=None,
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
    )
    collections_repository = FakeCollectionsRepository([definition_a, definition_b])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/collections", headers=_bearer("token-a"))

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["definition_id"] == "def-a"


def _definition(definition_id="def-a", identity_sub="sub-a", name="A's list", description=None):
    return CollectionDefinition(
        definition_id=definition_id,
        identity_sub=identity_sub,
        name=name,
        kind="filter_list",
        console_id=None,
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
        description=description,
    )


def test_save_definition_stores_the_supplied_game_ids():
    collections_repository = FakeCollectionsRepository(known_games={"g1", "g2"})
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/collections",
        json={"name": "Handpicked", "game_ids": ["g2", "g1"], "description": "Best of"},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    assert response.json()["description"] == "Best of"
    assert collections_repository.items["def-1"] == ("g2", "g1")


def test_save_definition_defaults_kind_so_a_handpicked_list_needs_no_spec():
    collections_repository = FakeCollectionsRepository(known_games={"g1"})
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/collections", json={"name": "Handpicked", "game_ids": ["g1"]}, headers=_bearer("token-a"))

    assert response.status_code == 201
    assert response.json()["kind"] == "filter_list"


def test_save_definition_persists_and_returns_a_filter_predicate():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/collections",
        json={
            "name": "Criterion-ish",
            "filter_predicate": {
                "op": "or",
                "nodes": [{"op": "genre_in", "values": ["RPG"]}, {"op": "score_at_least", "threshold": 70.0}],
            },
        },
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    assert response.json()["filter_predicate"] == {
        "op": "or",
        "nodes": [{"op": "genre_in", "values": ["RPG"]}, {"op": "score_at_least", "threshold": 70.0}],
    }
    saved = collections_repository.definitions["def-1"]
    assert saved.filter_predicate == Or(nodes=(GenreIn(values=("RPG",)), ScoreAtLeast(threshold=70.0)))


def test_save_definition_rejects_a_malformed_filter_predicate_as_400_not_500():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/collections",
        json={"name": "Bad", "filter_predicate": {"op": "and", "nodes": []}},
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400
    assert collections_repository.definitions == {}


def test_definition_with_no_filter_predicate_returns_null_not_an_empty_object():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/collections", json={"name": "Handpicked"}, headers=_bearer("token-a"))

    assert response.json()["filter_predicate"] is None


def test_save_definition_rejects_an_unknown_game_id():
    collections_repository = FakeCollectionsRepository(known_games={"g1"})
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/collections", json={"name": "Bad", "game_ids": ["g1", "g9"]}, headers=_bearer("token-a"))

    assert response.status_code == 400
    assert "g9" in response.json()["detail"]
    assert collections_repository.definitions == {}


def test_save_definition_rejects_a_malformed_game_id_as_400_not_500():
    collections_repository = FakeCollectionsRepository(malformed_game_ids={"not-a-uuid"})
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/collections", json={"name": "Bad", "game_ids": ["not-a-uuid"]}, headers=_bearer("token-a"))

    assert response.status_code == 400
    assert "UUID" in response.json()["detail"]


def test_save_definition_lower_cases_game_ids_before_validating_them():
    game_id = "550e8400-e29b-41d4-a716-446655440000"
    collections_repository = FakeCollectionsRepository(known_games={game_id})
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        "/collections", json={"name": "Shouty", "game_ids": [game_id.upper()]}, headers=_bearer("token-a")
    )

    assert response.status_code == 201
    assert collections_repository.items["def-1"] == (game_id,)


def test_get_definition_returns_its_items():
    collections_repository = FakeCollectionsRepository([_definition()], known_games={"g1"})
    collections_repository.items["def-a"] = ("g1",)
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/collections/def-a", headers=_bearer("token-a"))

    assert response.status_code == 200
    body = response.json()
    assert body["definition_id"] == "def-a"
    assert [item["game_id"] for item in body["items"]] == ["g1"]
    assert body["items"][0]["cover_image_url"] == "g1.png"


def test_get_definition_items_returns_a_page_and_the_total():
    collections_repository = FakeCollectionsRepository([_definition()], known_games={"g1", "g2", "g3"})
    collections_repository.items["def-a"] = ("g1", "g2", "g3")
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/collections/def-a/items?limit=2&offset=1", headers=_bearer("token-a"))

    assert response.status_code == 200
    body = response.json()
    assert [item["game_id"] for item in body["items"]] == ["g2", "g3"]
    assert body["total"] == 3, "total counts the whole collection, not the page"


def test_get_definition_items_filters_by_title():
    collections_repository = FakeCollectionsRepository([_definition()], known_games={"g1", "g2"})
    collections_repository.items["def-a"] = ("g1", "g2")
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/collections/def-a/items?q=g2", headers=_bearer("token-a"))

    assert [item["game_id"] for item in response.json()["items"]] == ["g2"]


def test_get_definition_items_rejects_an_unknown_sort_field():
    collections_repository = FakeCollectionsRepository([_definition()])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/collections/def-a/items?sort=game_id", headers=_bearer("token-a"))

    assert response.status_code == 422


def test_get_definition_items_not_owned_returns_404():
    collections_repository = FakeCollectionsRepository([_definition(identity_sub="sub-b")])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/collections/def-a/items", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_remove_definition_item_removes_only_that_title():
    collections_repository = FakeCollectionsRepository([_definition()], known_games={"g1", "g2"})
    collections_repository.items["def-a"] = ("g1", "g2")
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete("/collections/def-a/items/g1", headers=_bearer("token-a"))

    assert response.status_code == 204
    assert collections_repository.items["def-a"] == ("g2",)


def test_remove_definition_item_not_a_member_returns_404():
    collections_repository = FakeCollectionsRepository([_definition()], known_games={"g1"})
    collections_repository.items["def-a"] = ("g1",)
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete("/collections/def-a/items/g9", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_remove_definition_item_not_owned_returns_404_without_touching_the_collection():
    collections_repository = FakeCollectionsRepository([_definition(identity_sub="sub-b")], known_games={"g1"})
    collections_repository.items["def-a"] = ("g1",)
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete("/collections/def-a/items/g1", headers=_bearer("token-a"))

    assert response.status_code == 404
    assert collections_repository.items["def-a"] == ("g1",)


def test_get_definition_not_owned_returns_404():
    collections_repository = FakeCollectionsRepository([_definition(identity_sub="sub-b")])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/collections/def-a", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_patch_definition_renames_and_replaces_membership():
    collections_repository = FakeCollectionsRepository([_definition()], known_games={"g1", "g2"})
    collections_repository.items["def-a"] = ("g1",)
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.patch(
        "/collections/def-a", json={"name": "Renamed", "game_ids": ["g2"]}, headers=_bearer("token-a")
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    assert [item["game_id"] for item in response.json()["items"]] == ["g2"]
    assert collections_repository.items["def-a"] == ("g2",)


def test_patch_definition_leaves_omitted_fields_alone():
    collections_repository = FakeCollectionsRepository([_definition(description="Original")], known_games={"g1"})
    collections_repository.items["def-a"] = ("g1",)
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.patch("/collections/def-a", json={"name": "Renamed"}, headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json()["description"] == "Original"
    assert collections_repository.items["def-a"] == ("g1",)


def test_patch_definition_can_clear_a_description_with_an_explicit_null():
    collections_repository = FakeCollectionsRepository([_definition(description="Original")])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.patch("/collections/def-a", json={"description": None}, headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json()["description"] is None


def test_patch_definition_duplicate_name_returns_409():
    collections_repository = FakeCollectionsRepository([_definition()], duplicate_names={"Taken"})
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.patch("/collections/def-a", json={"name": "Taken"}, headers=_bearer("token-a"))

    assert response.status_code == 409


def test_patch_definition_not_owned_returns_404():
    collections_repository = FakeCollectionsRepository([_definition(identity_sub="sub-b")])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.patch("/collections/def-a", json={"name": "Mine now"}, headers=_bearer("token-a"))

    assert response.status_code == 404
    assert collections_repository.definitions["def-a"].name == "A's list"


def test_delete_definition_removes_it():
    collections_repository = FakeCollectionsRepository([_definition()])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete("/collections/def-a", headers=_bearer("token-a"))

    assert response.status_code == 204
    assert collections_repository.definitions == {}


def test_delete_definition_not_owned_returns_404():
    collections_repository = FakeCollectionsRepository([_definition(identity_sub="sub-b")])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete("/collections/def-a", headers=_bearer("token-a"))

    assert response.status_code == 404
    assert "def-a" in collections_repository.definitions


def test_set_visibility_changes_it_and_returns_the_updated_definition():
    collections_repository = FakeCollectionsRepository([_definition()])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put("/collections/def-a/visibility", json={"visibility": "public"}, headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json()["visibility"] == "public"
    assert collections_repository.definitions["def-a"].visibility == "public"


def test_set_visibility_rejects_an_unknown_value():
    collections_repository = FakeCollectionsRepository([_definition()])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put("/collections/def-a/visibility", json={"visibility": "everyone"}, headers=_bearer("token-a"))

    assert response.status_code == 400
    assert collections_repository.definitions["def-a"].visibility == "private"


def test_set_visibility_not_owned_returns_404():
    collections_repository = FakeCollectionsRepository([_definition(identity_sub="sub-b")])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put("/collections/def-a/visibility", json={"visibility": "public"}, headers=_bearer("token-a"))

    assert response.status_code == 404


def test_follow_a_public_collection():
    other = replace(_definition(identity_sub="sub-b"), visibility="public")
    collections_repository = FakeCollectionsRepository([other])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/collections/def-a/follow", headers=_bearer("token-a"))

    assert response.status_code == 204
    assert collections_repository.collection_follows["def-a"] == {"sub-a"}


def test_cannot_follow_your_own_collection():
    collections_repository = FakeCollectionsRepository([replace(_definition(), visibility="public")])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/collections/def-a/follow", headers=_bearer("token-a"))

    assert response.status_code == 400
    assert collections_repository.collection_follows.get("def-a", set()) == set()


def test_cannot_follow_a_private_collection():
    other = _definition(identity_sub="sub-b")
    collections_repository = FakeCollectionsRepository([other])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/collections/def-a/follow", headers=_bearer("token-a"))

    assert response.status_code == 404
    assert collections_repository.collection_follows.get("def-a", set()) == set()


def test_follow_unknown_collection_is_404():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/collections/nonexistent/follow", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_unfollow_a_collection():
    other = replace(_definition(identity_sub="sub-b"), visibility="public")
    collections_repository = FakeCollectionsRepository([other])
    collections_repository.collection_follows["def-a"] = {"sub-a"}
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete("/collections/def-a/follow", headers=_bearer("token-a"))

    assert response.status_code == 204
    assert collections_repository.collection_follows["def-a"] == set()


def test_unfollow_is_idempotent_even_for_an_unknown_collection():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete("/collections/nonexistent/follow", headers=_bearer("token-a"))

    assert response.status_code == 204


def test_lists_followed_collections():
    followed = replace(_definition(definition_id="def-followed", identity_sub="sub-b"), visibility="public")
    not_followed = replace(_definition(definition_id="def-not-followed", identity_sub="sub-b"), visibility="public")
    collections_repository = FakeCollectionsRepository([followed, not_followed])
    collections_repository.collection_follows["def-followed"] = {"sub-a"}
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/collections/followed", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert [d["definition_id"] for d in response.json()] == ["def-followed"]


def test_run_definition_does_not_change_stored_membership():
    collections_repository = FakeCollectionsRepository([_definition()])
    collections_repository.items["def-a"] = ("g1",)
    candidate = GameCandidate(
        game_id="g2",
        title="Different Game",
        genre="Action",
        aaa_tier="AAA",
        franchise="",
        composite_score=90.0,
        rank_score=3,
        size_gb=50.0,
    )
    orchestrator = FakeOrchestrator(result=CollectionResult(included=(candidate,), excluded=(), used_gb=None))
    client, validator = _build(orchestrator, collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/collections/def-a/runs", headers=_bearer("token-a"))

    assert response.status_code == 201
    assert collections_repository.items["def-a"] == ("g1",)


def test_run_definition_does_not_resolve_completion_at_request_time():
    collections_repository = FakeCollectionsRepository([_definition()])
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator, collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    client.post("/collections/def-a/runs", headers=_bearer("token-a"))

    _, _, completion_map, completion_available = orchestrator.generate_calls[0]
    assert completion_map is None
    assert completion_available is False


def test_run_definition_not_found_returns_404():
    client, validator = _build()
    validator.register("token-a", _claims())

    response = client.post("/collections/unknown/runs", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_run_definition_not_owned_returns_404():
    definition = CollectionDefinition(
        definition_id="def-a",
        identity_sub="sub-b",
        name="B's list",
        kind="filter_list",
        console_id=None,
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
    )
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/collections/def-a/runs", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_run_definition_generates_and_persists():
    definition = CollectionDefinition(
        definition_id="def-a",
        identity_sub="sub-a",
        name="My RPGs",
        kind="filter_list",
        console_id=None,
        genre_filter=("RPG",),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
    )
    candidate = GameCandidate(
        game_id="g1",
        title="God of War",
        genre="Action",
        aaa_tier="AAA",
        franchise="God of War",
        composite_score=90.0,
        rank_score=3,
        size_gb=50.0,
    )
    orchestrator = FakeOrchestrator(result=CollectionResult(included=(candidate,), excluded=(), used_gb=None))
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(orchestrator, collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/collections/def-a/runs", headers=_bearer("token-a"))

    assert response.status_code == 201
    body = response.json()
    assert body["run_id"] == "run-1"
    assert body["included"][0]["game_id"] == "g1"
    assert len(collections_repository.saved_runs) == 1
    assert orchestrator.generate_calls[0][1].genre_filter == ("RPG",)
