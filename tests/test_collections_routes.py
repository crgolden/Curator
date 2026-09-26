"""Tests for POST /collections/preview, using create_app() with fake CatalogRepository/CollectionOrchestrator."""

from __future__ import annotations

import uuid
from dataclasses import replace

import psycopg
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from curator import collections_routes
from curator.app import create_app
from curator.collections.collection_orchestrator import (
    IGNORED_FILTER_MIN_PERCENT_COMPLETED,
    IGNORED_FILTER_REASON_NO_TROPHY_DATA,
    CollectionResult,
    IgnoredFilter,
)
from curator.collections.collection_spec import CAPACITY_FILL_KIND, FILTER_LIST_KIND
from curator.collections.filter_predicate import (
    AND_OP,
    PREDICATE_NODES_KEY,
    PREDICATE_OP_KEY,
    And,
    GenreIn,
    Or,
    ScoreAtLeast,
    TierIn,
    predicate_to_dict,
)
from curator.collections.game_candidate import DEFAULT_SIZE, MEASURED_SIZE, GameCandidate
from curator.collections.repository import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    CollectionDefinition,
    CollectionItem,
    UserConsole,
)
from curator.collections.sort_order import COMPOSITE_DESC
from curator.collections_routes import (
    CollectionItemsPageResponse,
    CollectionPreviewResponse,
    CollectionRunResponse,
    CollectionSpecRequest,
    DefinitionDetailResponse,
    DefinitionResponse,
    IgnoredFilterResponse,
    SaveDefinitionRequest,
    UpdateDefinitionRequest,
    VisibilityUpdateRequest,
)
from curator.persistence.crypto import TokenCrypto
from curator.psn.title_platform import PS5
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
from test_trophy_routes import FakeTrophyClient, FakeTrophyClientFactory
from test_values import new_game_id


def _spec(**fields: object) -> dict[str, object]:
    return CollectionSpecRequest.model_validate(fields).model_dump(exclude_unset=True)


def _save(**fields: object) -> dict[str, object]:
    return SaveDefinitionRequest.model_validate(fields).model_dump(exclude_unset=True)


def _update(**fields: object) -> dict[str, object]:
    return UpdateDefinitionRequest.model_validate(fields).model_dump(exclude_unset=True)


def _visibility(visibility: str) -> dict[str, object]:
    return VisibilityUpdateRequest(visibility=visibility).model_dump()


_DEFINITION_LIST = TypeAdapter(list[DefinitionResponse])


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
        self.saved_install_targets: list[str | None] = []
        self.retarget_calls: list[tuple[bool, str | None]] = []
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

    async def save_definition(
        self, identity_sub, name, spec, *, description=None, game_ids=(), install_target_console_id=None
    ):
        self.saved_install_targets.append(install_target_console_id)
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
            if definition.share_slug == share_slug and definition.visibility != VISIBILITY_PRIVATE:
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

    async def update_definition(
        self,
        definition_id,
        *,
        name,
        description,
        game_ids=None,
        install_target_console_id=None,
        retarget_install_console=False,
    ):
        self.retarget_calls.append((retarget_install_console, install_target_console_id))
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

    response = client.post(_path(client, collections_routes.preview_collection), json=_spec(kind=FILTER_LIST_KIND))

    assert response.status_code == 401


def test_invalid_kind_is_rejected():
    client, validator = _build()
    validator.register("token-a", _claims())

    response = client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=uuid.uuid4().hex),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400


def test_orchestrator_value_error_becomes_400():
    orchestrator = FakeOrchestrator(raises=ValueError("Unknown console_id"))
    client, validator = _build(orchestrator)
    validator.register("token-a", _claims())

    response = client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=CAPACITY_FILL_KIND, console_id="missing"),
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

    response = client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 200
    body = CollectionPreviewResponse.model_validate(response.json())
    assert body.used_gb == 50.0
    assert body.included[0].game_id == "g1"
    assert orchestrator.generate_calls[0][0] == "sub-a"
    assert orchestrator.generate_calls[0][1].kind == FILTER_LIST_KIND


def _candidate(game_id: str, *, aaa_tier: str | None = "AAA") -> GameCandidate:
    return GameCandidate(
        game_id=game_id,
        title=game_id,
        genre="Action",
        aaa_tier=aaa_tier,
        franchise="",
        composite_score=90.0,
        rank_score=3,
        size_gb=50.0,
    )


def test_preview_reports_an_ignored_completion_floor_and_the_silently_dropped_count():
    result = CollectionResult(
        included=(),
        excluded=(),
        used_gb=None,
        ignored_filters=(IgnoredFilter(IGNORED_FILTER_MIN_PERCENT_COMPLETED, IGNORED_FILTER_REASON_NO_TROPHY_DATA),),
        excluded_for_missing_trophy_data=3,
    )
    client, validator = _build(orchestrator=FakeOrchestrator(result))
    validator.register("token-a", _claims(sub="sub-a"))

    body = CollectionPreviewResponse.model_validate(
        client.post(
            _path(client, collections_routes.preview_collection),
            json=_spec(kind=FILTER_LIST_KIND, min_percent_completed=50),
            headers=_bearer("token-a"),
        ).json()
    )

    assert body.ignored_filters == [
        IgnoredFilterResponse(filter=IGNORED_FILTER_MIN_PERCENT_COMPLETED, reason=IGNORED_FILTER_REASON_NO_TROPHY_DATA)
    ]
    assert body.excluded_for_missing_trophy_data == 3


def test_preview_caps_both_lists_and_reports_their_real_totals():
    """The whole result is generated; only the body is capped. An ADVENTURE filter previously
    serialised 878 games into one response and the client rendered every one of them."""
    result = CollectionResult(
        included=tuple(_candidate(f"inc-{i}") for i in range(7)),
        excluded=tuple(_candidate(f"exc-{i}") for i in range(9)),
        used_gb=None,
    )
    client, validator = _build(FakeOrchestrator(result=result))
    validator.register("token-a", _claims(sub="sub-a"))

    body = CollectionPreviewResponse.model_validate(
        client.post(
            _path(client, collections_routes.preview_collection) + "?limit=2",
            json=_spec(kind=FILTER_LIST_KIND),
            headers=_bearer("token-a"),
        ).json()
    )

    assert [g.game_id for g in body.included] == ["inc-0", "inc-1"]
    assert [g.game_id for g in body.excluded] == ["exc-0", "exc-1"]
    assert body.included_total == 7
    assert body.excluded_total == 9


def test_preview_returns_every_included_id_even_when_the_body_is_capped():
    """Capping the display must not truncate what a save stores.

    ``POST /collections`` takes membership as a list of ids, and preview persists nothing, so this
    response is the only place the complete set exists. Paging the objects while paging the ids too
    would turn a presentational change into silent data loss on save.
    """
    result = CollectionResult(included=tuple(_candidate(f"inc-{i}") for i in range(7)), excluded=(), used_gb=None)
    client, validator = _build(FakeOrchestrator(result=result))
    validator.register("token-a", _claims(sub="sub-a"))

    body = CollectionPreviewResponse.model_validate(
        client.post(
            _path(client, collections_routes.preview_collection) + "?limit=2",
            json=_spec(kind=FILTER_LIST_KIND),
            headers=_bearer("token-a"),
        ).json()
    )

    assert len(body.included) == 2
    assert body.included_game_ids == [f"inc-{i}" for i in range(7)]


def test_preview_offset_pages_both_lists_together():
    """One offset drives both lists, which is what makes a single pager control meaningful over the pair."""
    result = CollectionResult(
        included=tuple(_candidate(f"inc-{i}") for i in range(5)),
        excluded=tuple(_candidate(f"exc-{i}") for i in range(5)),
        used_gb=None,
    )
    client, validator = _build(FakeOrchestrator(result=result))
    validator.register("token-a", _claims(sub="sub-a"))

    body = CollectionPreviewResponse.model_validate(
        client.post(
            _path(client, collections_routes.preview_collection) + "?limit=2&offset=4",
            json=_spec(kind=FILTER_LIST_KIND),
            headers=_bearer("token-a"),
        ).json()
    )

    assert [g.game_id for g in body.included] == ["inc-4"]
    assert [g.game_id for g in body.excluded] == ["exc-4"]
    assert body.included_total == 5


def test_preview_says_where_each_size_came_from():
    """A bare size_gb cannot tell a client whether 20 GB is a figure somebody measured or the flat
    fallback standing in for one, and only the latter is worth prompting its owner about."""
    measured_candidate = replace(_candidate(new_game_id()), size_source=MEASURED_SIZE)
    unknown_candidate = replace(_candidate(new_game_id()), size_source=DEFAULT_SIZE)
    result = CollectionResult(included=(measured_candidate, unknown_candidate), excluded=(), used_gb=None)
    client, validator = _build(FakeOrchestrator(result=result))
    validator.register("token-a", _claims(sub="sub-a"))

    body = CollectionPreviewResponse.model_validate(
        client.post(
            _path(client, collections_routes.preview_collection) + "",
            json=_spec(kind=FILTER_LIST_KIND),
            headers=_bearer("token-a"),
        ).json()
    )

    assert [game.size_source for game in body.included] == [MEASURED_SIZE, DEFAULT_SIZE]


def test_preview_rejects_a_page_size_above_the_ceiling():
    client, validator = _build(FakeOrchestrator())
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.preview_collection) + "?limit=101",
        json=_spec(kind=FILTER_LIST_KIND),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 422


def test_preview_renders_a_tier_less_game_as_null_not_empty_string():
    result = CollectionResult(included=(_candidate("g1", aaa_tier=None),), excluded=(), used_gb=None)
    client, validator = _build(FakeOrchestrator(result=result))
    validator.register("token-a", _claims(sub="sub-a"))

    body = CollectionPreviewResponse.model_validate(
        client.post(
            _path(client, collections_routes.preview_collection) + "",
            json=_spec(kind=FILTER_LIST_KIND),
            headers=_bearer("token-a"),
        ).json()
    )

    assert body.included[0].aaa_tier is None


def test_preview_passes_min_percent_completed_through_to_spec():
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator)
    validator.register("token-a", _claims(sub="sub-a"))

    client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND, min_percent_completed=50),
        headers=_bearer("token-a"),
    )

    assert orchestrator.generate_calls[0][1].min_percent_completed == 50


def test_preview_passes_sort_order_and_exclude_installed_on_through_to_spec():
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator)
    validator.register("token-a", _claims(sub="sub-a"))

    client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND, sort_order=COMPOSITE_DESC, exclude_installed_on=["c1", "c2"]),
        headers=_bearer("token-a"),
    )

    spec = orchestrator.generate_calls[0][1]
    assert spec.sort_order == COMPOSITE_DESC
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

    response = client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND),
        headers=_bearer("token-a"),
    )

    assert CollectionPreviewResponse.model_validate(response.json()).included[0].percent_completed == 87


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

    client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND),
        headers=_bearer("token-a"),
    )

    _, _, completion_map, completion_available = orchestrator.generate_calls[0]
    assert completion_map is None
    assert completion_available is False
    assert factory.calls == []


def test_preview_threads_the_completion_floor_through_to_the_spec():
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator)
    validator.register("token-a", _claims(sub="sub-a"))

    client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND, min_percent_completed=80),
        headers=_bearer("token-a"),
    )

    _, spec, _, _ = orchestrator.generate_calls[0]
    assert spec.min_percent_completed == 80


def test_preview_parses_a_filter_predicate_onto_the_spec():
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(
            kind=FILTER_LIST_KIND,
            filter_predicate=predicate_to_dict(
                Or(
                    nodes=(
                        GenreIn(values=("RPG",)),
                        And(nodes=(GenreIn(values=("Action",)), TierIn(values=("Indie",)))),
                    )
                )
            ),
        ),
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
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND, filter_predicate={PREDICATE_OP_KEY: uuid.uuid4().hex}),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400


def test_save_definition_rejects_invalid_kind():
    client, validator = _build()
    validator.register("token-a", _claims())

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name="x", kind=uuid.uuid4().hex),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400


def test_save_definition_persists_and_returns_it():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name="My RPGs", kind=FILTER_LIST_KIND, genre_filter=["RPG"], min_score=80.0),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    body = DefinitionResponse.model_validate(response.json())
    assert body.name == "My RPGs"
    assert body.genre_filter == ["RPG"]
    assert len(collections_repository.definitions) == 1


def test_save_definition_rejects_a_console_the_caller_does_not_own():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))
    unowned_console_id = uuid.uuid4().hex

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name="Someone else's PS5", kind=CAPACITY_FILL_KIND, console_id=unowned_console_id),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == collections_routes.unknown_console_detail(unowned_console_id)
    assert collections_repository.definitions == {}


def test_save_definition_rejects_an_exclude_installed_on_console_the_caller_does_not_own():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))
    unowned_console_id = uuid.uuid4().hex

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name="Not on my Vita", kind=FILTER_LIST_KIND, exclude_installed_on=[unowned_console_id]),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == collections_routes.unknown_excluded_consoles_detail([unowned_console_id])
    assert collections_repository.definitions == {}


def test_save_definition_round_trips_sort_order_and_exclude_installed_on():
    console = UserConsole(
        console_id="console-a",
        name="Living room PS5",
        platform=PS5,
        raw_capacity_gb=800.0,
        update_buffer_gb=50.0,
        routing_genres=(),
        fill_order=0,
    )
    collections_repository = FakeCollectionsRepository(consoles=[console])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(
            name="Not on my PS5",
            kind=FILTER_LIST_KIND,
            sort_order=COMPOSITE_DESC,
            exclude_installed_on=["console-a"],
        ),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    body = DefinitionResponse.model_validate(response.json())
    assert body.sort_order == COMPOSITE_DESC
    assert body.exclude_installed_on == ["console-a"]


def test_save_definition_accepts_a_console_the_caller_owns():
    console = UserConsole(
        console_id="console-a",
        name="Living room PS5",
        platform=PS5,
        raw_capacity_gb=800.0,
        update_buffer_gb=50.0,
        routing_genres=(),
        fill_order=0,
    )
    collections_repository = FakeCollectionsRepository(consoles=[console])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name="PS5 fill", kind=CAPACITY_FILL_KIND, console_id="console-a"),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    assert DefinitionResponse.model_validate(response.json()).console_id == "console-a"


def test_save_definition_persists_min_percent_completed():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name="Nearly Done", kind=FILTER_LIST_KIND, min_percent_completed=75),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    assert DefinitionResponse.model_validate(response.json()).min_percent_completed == 75
    assert collections_repository.definitions["def-1"].min_percent_completed == 75


def test_save_definition_duplicate_name_returns_409():
    collections_repository = FakeCollectionsRepository(duplicate_names={"My RPGs"})
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name="My RPGs", kind=FILTER_LIST_KIND),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 409
    assert "My RPGs" in response.json()["detail"]


def test_list_definitions_scopes_to_caller():
    definition_a = CollectionDefinition(
        definition_id="def-a",
        identity_sub="sub-a",
        name="A's list",
        kind=FILTER_LIST_KIND,
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
        kind=FILTER_LIST_KIND,
        console_id=None,
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
    )
    collections_repository = FakeCollectionsRepository([definition_a, definition_b])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, collections_routes.list_definitions), headers=_bearer("token-a"))

    assert response.status_code == 200
    body = _DEFINITION_LIST.validate_python(response.json())
    assert len(body) == 1
    assert body[0].definition_id == "def-a"


def _definition(definition_id="def-a", identity_sub="sub-a", name="A's list", description=None):
    return CollectionDefinition(
        definition_id=definition_id,
        identity_sub=identity_sub,
        name=name,
        kind=FILTER_LIST_KIND,
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
        _path(client, collections_routes.save_definition),
        json=_save(name="Handpicked", game_ids=["g2", "g1"], description="Best of"),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    assert DefinitionResponse.model_validate(response.json()).description == "Best of"
    assert collections_repository.items["def-1"] == ("g2", "g1")


def test_save_definition_defaults_kind_so_a_handpicked_list_needs_no_spec():
    collections_repository = FakeCollectionsRepository(known_games={"g1"})
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name="Handpicked", game_ids=["g1"]),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    assert DefinitionResponse.model_validate(response.json()).kind == FILTER_LIST_KIND


def test_save_definition_persists_and_returns_a_filter_predicate():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(
            name="Criterion-ish",
            filter_predicate=predicate_to_dict(Or(nodes=(GenreIn(values=("RPG",)), ScoreAtLeast(threshold=70.0)))),
        ),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    assert DefinitionResponse.model_validate(response.json()).filter_predicate == predicate_to_dict(
        Or(nodes=(GenreIn(values=("RPG",)), ScoreAtLeast(threshold=70.0)))
    )
    saved = collections_repository.definitions["def-1"]
    assert saved.filter_predicate == Or(nodes=(GenreIn(values=("RPG",)), ScoreAtLeast(threshold=70.0)))


def test_save_definition_rejects_a_malformed_filter_predicate_as_400_not_500():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name="Bad", filter_predicate={PREDICATE_OP_KEY: AND_OP, PREDICATE_NODES_KEY: []}),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400
    assert collections_repository.definitions == {}


def test_definition_with_no_filter_predicate_returns_null_not_an_empty_object():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.save_definition), json=_save(name="Handpicked"), headers=_bearer("token-a")
    )

    assert DefinitionResponse.model_validate(response.json()).filter_predicate is None


def test_save_definition_rejects_an_unknown_game_id():
    collections_repository = FakeCollectionsRepository(known_games={"g1"})
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name="Bad", game_ids=["g1", "g9"]),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400
    assert "g9" in response.json()["detail"]
    assert collections_repository.definitions == {}


def test_save_definition_rejects_a_malformed_game_id_as_400_not_500():
    collections_repository = FakeCollectionsRepository(malformed_game_ids={"not-a-uuid"})
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name="Bad", game_ids=["not-a-uuid"]),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400
    assert "UUID" in response.json()["detail"]


def test_save_definition_lower_cases_game_ids_before_validating_them():
    game_id = "550e8400-e29b-41d4-a716-446655440000"
    collections_repository = FakeCollectionsRepository(known_games={game_id})
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name="Shouty", game_ids=[game_id.upper()]),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 201
    assert collections_repository.items["def-1"] == (game_id,)


def test_get_definition_returns_its_items():
    collections_repository = FakeCollectionsRepository([_definition()], known_games={"g1"})
    collections_repository.items["def-a"] = ("g1",)
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(
        _path(client, collections_routes.get_definition, definition_id="def-a"), headers=_bearer("token-a")
    )

    assert response.status_code == 200
    body = DefinitionDetailResponse.model_validate(response.json())
    assert body.definition_id == "def-a"
    assert [item.game_id for item in body.items] == ["g1"]
    assert body.items[0].cover_image_url == "g1.png"


def test_get_definition_items_returns_a_page_and_the_total():
    collections_repository = FakeCollectionsRepository([_definition()], known_games={"g1", "g2", "g3"})
    collections_repository.items["def-a"] = ("g1", "g2", "g3")
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(
        _path(client, collections_routes.get_definition_items, definition_id="def-a") + "?limit=2&offset=1",
        headers=_bearer("token-a"),
    )

    assert response.status_code == 200
    body = CollectionItemsPageResponse.model_validate(response.json())
    assert [item.game_id for item in body.items] == ["g2", "g3"]
    assert body.total == 3, "total counts the whole collection, not the page"


def test_get_definition_items_filters_by_title():
    collections_repository = FakeCollectionsRepository([_definition()], known_games={"g1", "g2"})
    collections_repository.items["def-a"] = ("g1", "g2")
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(
        _path(client, collections_routes.get_definition_items, definition_id="def-a") + "?q=g2",
        headers=_bearer("token-a"),
    )

    assert [item.game_id for item in CollectionItemsPageResponse.model_validate(response.json()).items] == ["g2"]


def test_get_definition_items_rejects_an_unknown_sort_field():
    collections_repository = FakeCollectionsRepository([_definition()])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(
        _path(client, collections_routes.get_definition_items, definition_id="def-a") + "?sort=game_id",
        headers=_bearer("token-a"),
    )

    assert response.status_code == 422


def test_get_definition_items_not_owned_returns_404():
    collections_repository = FakeCollectionsRepository([_definition(identity_sub="sub-b")])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(
        _path(client, collections_routes.get_definition_items, definition_id="def-a"), headers=_bearer("token-a")
    )

    assert response.status_code == 404


def test_remove_definition_item_removes_only_that_title():
    collections_repository = FakeCollectionsRepository([_definition()], known_games={"g1", "g2"})
    collections_repository.items["def-a"] = ("g1", "g2")
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete(
        _path(client, collections_routes.remove_definition_item, definition_id="def-a", game_id="g1"),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 204
    assert collections_repository.items["def-a"] == ("g2",)


def test_remove_definition_item_not_a_member_returns_404():
    collections_repository = FakeCollectionsRepository([_definition()], known_games={"g1"})
    collections_repository.items["def-a"] = ("g1",)
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete(
        _path(client, collections_routes.remove_definition_item, definition_id="def-a", game_id="g9"),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 404


def test_remove_definition_item_not_owned_returns_404_without_touching_the_collection():
    collections_repository = FakeCollectionsRepository([_definition(identity_sub="sub-b")], known_games={"g1"})
    collections_repository.items["def-a"] = ("g1",)
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete(
        _path(client, collections_routes.remove_definition_item, definition_id="def-a", game_id="g1"),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 404
    assert collections_repository.items["def-a"] == ("g1",)


def test_get_definition_not_owned_returns_404():
    collections_repository = FakeCollectionsRepository([_definition(identity_sub="sub-b")])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(
        _path(client, collections_routes.get_definition, definition_id="def-a"), headers=_bearer("token-a")
    )

    assert response.status_code == 404


def test_patch_definition_renames_and_replaces_membership():
    collections_repository = FakeCollectionsRepository([_definition()], known_games={"g1", "g2"})
    collections_repository.items["def-a"] = ("g1",)
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.patch(
        _path(client, collections_routes.update_definition, definition_id="def-a"),
        json=_update(name="Renamed", game_ids=["g2"]),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 200
    detail = DefinitionDetailResponse.model_validate(response.json())
    assert detail.name == "Renamed"
    assert [item.game_id for item in detail.items] == ["g2"]
    assert collections_repository.items["def-a"] == ("g2",)


def test_patch_definition_leaves_omitted_fields_alone():
    collections_repository = FakeCollectionsRepository([_definition(description="Original")], known_games={"g1"})
    collections_repository.items["def-a"] = ("g1",)
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.patch(
        _path(client, collections_routes.update_definition, definition_id="def-a"),
        json=_update(name="Renamed"),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 200
    assert DefinitionResponse.model_validate(response.json()).description == "Original"
    assert collections_repository.items["def-a"] == ("g1",)


def test_patch_definition_can_clear_a_description_with_an_explicit_null():
    collections_repository = FakeCollectionsRepository([_definition(description="Original")])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.patch(
        _path(client, collections_routes.update_definition, definition_id="def-a"),
        json=_update(description=None),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 200
    assert DefinitionResponse.model_validate(response.json()).description is None


def test_patch_definition_duplicate_name_returns_409():
    collections_repository = FakeCollectionsRepository([_definition()], duplicate_names={"Taken"})
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.patch(
        _path(client, collections_routes.update_definition, definition_id="def-a"),
        json=_update(name="Taken"),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 409


def test_patch_definition_not_owned_returns_404():
    collections_repository = FakeCollectionsRepository([_definition(identity_sub="sub-b")])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.patch(
        _path(client, collections_routes.update_definition, definition_id="def-a"),
        json=_update(name="Mine now"),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 404
    assert collections_repository.definitions["def-a"].name == "A's list"


def test_delete_definition_removes_it():
    collections_repository = FakeCollectionsRepository([_definition()])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete(
        _path(client, collections_routes.delete_definition, definition_id="def-a"), headers=_bearer("token-a")
    )

    assert response.status_code == 204
    assert collections_repository.definitions == {}


def test_delete_definition_not_owned_returns_404():
    collections_repository = FakeCollectionsRepository([_definition(identity_sub="sub-b")])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete(
        _path(client, collections_routes.delete_definition, definition_id="def-a"), headers=_bearer("token-a")
    )

    assert response.status_code == 404
    assert "def-a" in collections_repository.definitions


def test_set_visibility_changes_it_and_returns_the_updated_definition():
    collections_repository = FakeCollectionsRepository([_definition()])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put(
        _path(client, collections_routes.set_visibility, definition_id="def-a"),
        json=_visibility(VISIBILITY_PUBLIC),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 200
    assert DefinitionResponse.model_validate(response.json()).visibility == VISIBILITY_PUBLIC
    assert collections_repository.definitions["def-a"].visibility == VISIBILITY_PUBLIC


def test_set_visibility_rejects_an_unknown_value():
    collections_repository = FakeCollectionsRepository([_definition()])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put(
        _path(client, collections_routes.set_visibility, definition_id="def-a"),
        json=_visibility(uuid.uuid4().hex),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400
    assert collections_repository.definitions["def-a"].visibility == VISIBILITY_PRIVATE


def test_set_visibility_not_owned_returns_404():
    collections_repository = FakeCollectionsRepository([_definition(identity_sub="sub-b")])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put(
        _path(client, collections_routes.set_visibility, definition_id="def-a"),
        json=_visibility(VISIBILITY_PUBLIC),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 404


def test_follow_a_public_collection():
    other = replace(_definition(identity_sub="sub-b"), visibility=VISIBILITY_PUBLIC)
    collections_repository = FakeCollectionsRepository([other])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.follow_definition, definition_id="def-a"), headers=_bearer("token-a")
    )

    assert response.status_code == 204
    assert collections_repository.collection_follows["def-a"] == {"sub-a"}


def test_cannot_follow_your_own_collection():
    collections_repository = FakeCollectionsRepository([replace(_definition(), visibility=VISIBILITY_PUBLIC)])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.follow_definition, definition_id="def-a"), headers=_bearer("token-a")
    )

    assert response.status_code == 400
    assert collections_repository.collection_follows.get("def-a", set()) == set()


def test_cannot_follow_a_private_collection():
    other = _definition(identity_sub="sub-b")
    collections_repository = FakeCollectionsRepository([other])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.follow_definition, definition_id="def-a"), headers=_bearer("token-a")
    )

    assert response.status_code == 404
    assert collections_repository.collection_follows.get("def-a", set()) == set()


def test_follow_unknown_collection_is_404():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.follow_definition, definition_id="nonexistent"), headers=_bearer("token-a")
    )

    assert response.status_code == 404


def test_unfollow_a_collection():
    other = replace(_definition(identity_sub="sub-b"), visibility=VISIBILITY_PUBLIC)
    collections_repository = FakeCollectionsRepository([other])
    collections_repository.collection_follows["def-a"] = {"sub-a"}
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete(
        _path(client, collections_routes.unfollow_definition, definition_id="def-a"), headers=_bearer("token-a")
    )

    assert response.status_code == 204
    assert collections_repository.collection_follows["def-a"] == set()


def test_unfollow_is_idempotent_even_for_an_unknown_collection():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.delete(
        _path(client, collections_routes.unfollow_definition, definition_id="nonexistent"), headers=_bearer("token-a")
    )

    assert response.status_code == 204


def test_lists_followed_collections():
    followed = replace(_definition(definition_id="def-followed", identity_sub="sub-b"), visibility=VISIBILITY_PUBLIC)
    not_followed = replace(
        _definition(definition_id="def-not-followed", identity_sub="sub-b"), visibility=VISIBILITY_PUBLIC
    )
    collections_repository = FakeCollectionsRepository([followed, not_followed])
    collections_repository.collection_follows["def-followed"] = {"sub-a"}
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, collections_routes.list_followed_collections), headers=_bearer("token-a"))

    assert response.status_code == 200
    assert [d.definition_id for d in _DEFINITION_LIST.validate_python(response.json())] == ["def-followed"]


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

    response = client.post(
        _path(client, collections_routes.run_definition, definition_id="def-a"), headers=_bearer("token-a")
    )

    assert response.status_code == 201
    assert collections_repository.items["def-a"] == ("g1",)


def test_run_definition_does_not_resolve_completion_at_request_time():
    collections_repository = FakeCollectionsRepository([_definition()])
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator, collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    client.post(_path(client, collections_routes.run_definition, definition_id="def-a"), headers=_bearer("token-a"))

    _, _, completion_map, completion_available = orchestrator.generate_calls[0]
    assert completion_map is None
    assert completion_available is False


def test_run_definition_not_found_returns_404():
    client, validator = _build()
    validator.register("token-a", _claims())

    response = client.post(
        _path(client, collections_routes.run_definition, definition_id="unknown"), headers=_bearer("token-a")
    )

    assert response.status_code == 404


def test_run_definition_not_owned_returns_404():
    definition = CollectionDefinition(
        definition_id="def-a",
        identity_sub="sub-b",
        name="B's list",
        kind=FILTER_LIST_KIND,
        console_id=None,
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
    )
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(
        _path(client, collections_routes.run_definition, definition_id="def-a"), headers=_bearer("token-a")
    )

    assert response.status_code == 404


def test_run_definition_generates_and_persists():
    definition = CollectionDefinition(
        definition_id="def-a",
        identity_sub="sub-a",
        name="My RPGs",
        kind=FILTER_LIST_KIND,
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

    response = client.post(
        _path(client, collections_routes.run_definition, definition_id="def-a"), headers=_bearer("token-a")
    )

    assert response.status_code == 201
    body = CollectionRunResponse.model_validate(response.json())
    assert body.run_id == "run-1"
    assert body.included[0].game_id == "g1"
    assert len(collections_repository.saved_runs) == 1
    assert orchestrator.generate_calls[0][1].genre_filter == ("RPG",)


def test_run_caps_the_response_but_still_persists_every_result():
    """The cap is presentational. Truncating what `save_run` stores would silently discard run history,
    which is the opposite of what bounding the response body is for -- and would foreclose ever serving
    later pages from the stored run.
    """
    definition = CollectionDefinition(
        definition_id="def-a",
        identity_sub="sub-a",
        name="My RPGs",
        kind=FILTER_LIST_KIND,
        console_id=None,
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
    )
    result = CollectionResult(
        included=tuple(_candidate(f"inc-{i}") for i in range(6)),
        excluded=tuple(_candidate(f"exc-{i}") for i in range(4)),
        used_gb=None,
    )
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(FakeOrchestrator(result=result), collections_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    body = CollectionRunResponse.model_validate(
        client.post(
            _path(client, collections_routes.run_definition, definition_id="def-a") + "?limit=2",
            headers=_bearer("token-a"),
        ).json()
    )

    assert [g.game_id for g in body.included] == ["inc-0", "inc-1"]
    assert body.included_total == 6
    assert body.excluded_total == 4

    _, _, _, saved_included, saved_excluded = collections_repository.saved_runs[0]
    assert len(saved_included) == 6
    assert len(saved_excluded) == 4
