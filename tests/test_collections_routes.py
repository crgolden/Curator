"""Tests for POST /collections/preview, using create_app() with fake CatalogRepository/CollectionOrchestrator."""

from __future__ import annotations

import uuid
from dataclasses import replace
from http import HTTPStatus

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
from curator.query_params import LIMIT_PARAM, OFFSET_PARAM, SORT_PARAM
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
from test_values import (
    lowercase_token,
    new_console_id,
    new_cover_image_url,
    new_definition_id,
    new_game_id,
    new_game_title,
    new_genre_name,
    new_identity_sub,
    new_opaque_token,
    new_percent_completed,
    new_positive_count,
    new_review_score,
    new_run_id,
    new_share_slug,
    new_size_gb,
    new_small_count,
)


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
        self.collection_follows: dict[str, set[str]] = {}
        self.cover_image_url = new_cover_image_url()
        self.run_id = new_run_id()

    async def list_user_consoles(self, identity_sub):
        return self.consoles

    async def list_candidates(self, identity_sub, *, platform=None, include_inactive=False, min_percent_completed=None):
        return self._candidates

    async def existing_game_ids(self, game_ids):
        if self.malformed_game_ids & set(game_ids):
            raise psycopg.errors.InvalidTextRepresentation(lowercase_token())
        return {game_id for game_id in game_ids if game_id in self.known_games}

    async def save_definition(
        self, identity_sub, name, spec, *, description=None, game_ids=(), install_target_console_id=None
    ):
        self.saved_install_targets.append(install_target_console_id)
        if name in self.duplicate_names:
            raise psycopg.errors.UniqueViolation(lowercase_token())
        definition_id = new_definition_id()
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
            share_slug=new_share_slug(),
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
                title=game_id,
                franchise=None,
                genre=None,
                aaa_tier=None,
                critical_score=None,
                oc_score=None,
                psn_rating=None,
                cover_image_url=self.cover_image_url,
                owner_has_access=True,
            )
            for rank, game_id in enumerate(self.items.get(definition_id, ()), start=1)
        ]

    async def list_definition_items_page(
        self, definition_id, *, search=None, genre=None, sort=None, sort_dir=None, limit, offset=0
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
            raise psycopg.errors.UniqueViolation(lowercase_token())
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
        return self.run_id


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


def _bearer_for(validator, sub: str) -> dict[str, str]:
    token = new_opaque_token()
    validator.register(token, _claims(sub=sub))
    return _bearer(token)


def _candidate(**overrides):
    return replace(
        GameCandidate(
            game_id=new_game_id(),
            title=new_game_title(),
            genre=new_genre_name(),
            aaa_tier=new_genre_name(),
            franchise=new_game_title(),
            composite_score=new_review_score(),
            rank_score=new_small_count(),
            size_gb=new_size_gb(),
        ),
        **overrides,
    )


def _candidates(count: int) -> tuple[GameCandidate, ...]:
    return tuple(_candidate() for _ in range(count))


def _definition(**overrides):
    return replace(
        CollectionDefinition(
            definition_id=new_definition_id(),
            identity_sub=new_identity_sub(),
            name=new_game_title(),
            kind=FILTER_LIST_KIND,
            console_id=None,
            genre_filter=(),
            min_score=None,
            aaa_tier_filter=None,
            sort_order=None,
        ),
        **overrides,
    )


def _console(console_id: str) -> UserConsole:
    return UserConsole(
        console_id=console_id,
        name=new_game_title(),
        platform=PS5,
        raw_capacity_gb=new_size_gb(),
        update_buffer_gb=new_size_gb(),
        routing_genres=(),
        fill_order=0,
    )


def test_requires_bearer_token():
    client, _validator = _build()

    response = client.post(_path(client, collections_routes.preview_collection), json=_spec(kind=FILTER_LIST_KIND))

    assert response.status_code == HTTPStatus.UNAUTHORIZED


def test_invalid_kind_is_rejected():
    client, validator = _build()

    response = client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=uuid.uuid4().hex),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_orchestrator_value_error_becomes_400():
    error_message = lowercase_token()
    orchestrator = FakeOrchestrator(raises=ValueError(error_message))
    client, validator = _build(orchestrator)

    response = client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=CAPACITY_FILL_KIND, console_id=new_console_id()),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert error_message in response.json()["detail"]


def test_returns_generated_candidates():
    candidate = _candidate()
    used_gb = new_size_gb()
    result = CollectionResult(included=(candidate,), excluded=(), used_gb=used_gb)
    orchestrator = FakeOrchestrator(result=result)
    client, validator = _build(orchestrator)
    caller_sub = new_identity_sub()

    response = client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND),
        headers=_bearer_for(validator, caller_sub),
    )

    assert response.status_code == HTTPStatus.OK
    body = CollectionPreviewResponse.model_validate(response.json())
    assert body.used_gb == used_gb
    assert body.included[0].game_id == candidate.game_id
    assert orchestrator.generate_calls[0][0] == caller_sub
    assert orchestrator.generate_calls[0][1].kind == FILTER_LIST_KIND


def test_preview_reports_an_ignored_completion_floor_and_the_silently_dropped_count():
    dropped_count = new_positive_count()
    result = CollectionResult(
        included=(),
        excluded=(),
        used_gb=None,
        ignored_filters=(IgnoredFilter(IGNORED_FILTER_MIN_PERCENT_COMPLETED, IGNORED_FILTER_REASON_NO_TROPHY_DATA),),
        excluded_for_missing_trophy_data=dropped_count,
    )
    client, validator = _build(orchestrator=FakeOrchestrator(result))

    body = CollectionPreviewResponse.model_validate(
        client.post(
            _path(client, collections_routes.preview_collection),
            json=_spec(kind=FILTER_LIST_KIND, min_percent_completed=new_percent_completed()),
            headers=_bearer_for(validator, new_identity_sub()),
        ).json()
    )

    assert body.ignored_filters == [
        IgnoredFilterResponse(filter=IGNORED_FILTER_MIN_PERCENT_COMPLETED, reason=IGNORED_FILTER_REASON_NO_TROPHY_DATA)
    ]
    assert body.excluded_for_missing_trophy_data == dropped_count


def test_preview_caps_both_lists_and_reports_their_real_totals():
    """The whole result is generated; only the body is capped. An ADVENTURE filter previously
    serialised 878 games into one response and the client rendered every one of them."""
    limit = new_small_count()
    included = _candidates(limit + new_small_count())
    excluded = _candidates(limit + new_small_count())
    result = CollectionResult(included=included, excluded=excluded, used_gb=None)
    client, validator = _build(FakeOrchestrator(result=result))

    body = CollectionPreviewResponse.model_validate(
        client.post(
            _path(client, collections_routes.preview_collection),
            params={LIMIT_PARAM: limit},
            json=_spec(kind=FILTER_LIST_KIND),
            headers=_bearer_for(validator, new_identity_sub()),
        ).json()
    )

    assert [g.game_id for g in body.included] == [g.game_id for g in included[:limit]]
    assert [g.game_id for g in body.excluded] == [g.game_id for g in excluded[:limit]]
    assert body.included_total == len(included)
    assert body.excluded_total == len(excluded)


def test_preview_returns_every_included_id_even_when_the_body_is_capped():
    """Capping the display must not truncate what a save stores.

    ``POST /collections`` takes membership as a list of ids, and preview persists nothing, so this
    response is the only place the complete set exists. Paging the objects while paging the ids too
    would turn a presentational change into silent data loss on save.
    """
    limit = new_small_count()
    included = _candidates(limit + new_small_count())
    result = CollectionResult(included=included, excluded=(), used_gb=None)
    client, validator = _build(FakeOrchestrator(result=result))

    body = CollectionPreviewResponse.model_validate(
        client.post(
            _path(client, collections_routes.preview_collection),
            params={LIMIT_PARAM: limit},
            json=_spec(kind=FILTER_LIST_KIND),
            headers=_bearer_for(validator, new_identity_sub()),
        ).json()
    )

    assert len(body.included) == limit
    assert body.included_game_ids == [g.game_id for g in included]


def test_preview_offset_pages_both_lists_together():
    """One offset drives both lists, which is what makes a single pager control meaningful over the pair."""
    count = new_small_count()
    limit_past_the_last_row = new_small_count() + 1
    included = _candidates(count)
    excluded = _candidates(count)
    result = CollectionResult(included=included, excluded=excluded, used_gb=None)
    client, validator = _build(FakeOrchestrator(result=result))

    body = CollectionPreviewResponse.model_validate(
        client.post(
            _path(client, collections_routes.preview_collection),
            params={LIMIT_PARAM: limit_past_the_last_row, OFFSET_PARAM: count - 1},
            json=_spec(kind=FILTER_LIST_KIND),
            headers=_bearer_for(validator, new_identity_sub()),
        ).json()
    )

    assert [g.game_id for g in body.included] == [included[-1].game_id]
    assert [g.game_id for g in body.excluded] == [excluded[-1].game_id]
    assert body.included_total == count


def test_preview_says_where_each_size_came_from():
    """A bare size_gb cannot tell a client whether 20 GB is a figure somebody measured or the flat
    fallback standing in for one, and only the latter is worth prompting its owner about."""
    measured_candidate = _candidate(size_source=MEASURED_SIZE)
    unknown_candidate = _candidate(size_source=DEFAULT_SIZE)
    result = CollectionResult(included=(measured_candidate, unknown_candidate), excluded=(), used_gb=None)
    client, validator = _build(FakeOrchestrator(result=result))

    body = CollectionPreviewResponse.model_validate(
        client.post(
            _path(client, collections_routes.preview_collection) + "",
            json=_spec(kind=FILTER_LIST_KIND),
            headers=_bearer_for(validator, new_identity_sub()),
        ).json()
    )

    assert [game.size_source for game in body.included] == [MEASURED_SIZE, DEFAULT_SIZE]


def test_preview_rejects_a_page_size_above_the_ceiling():
    client, validator = _build(FakeOrchestrator())

    response = client.post(
        _path(client, collections_routes.preview_collection),
        params={LIMIT_PARAM: 101},
        json=_spec(kind=FILTER_LIST_KIND),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_preview_renders_a_tier_less_game_as_null_not_empty_string():
    result = CollectionResult(included=(_candidate(aaa_tier=None),), excluded=(), used_gb=None)
    client, validator = _build(FakeOrchestrator(result=result))

    body = CollectionPreviewResponse.model_validate(
        client.post(
            _path(client, collections_routes.preview_collection) + "",
            json=_spec(kind=FILTER_LIST_KIND),
            headers=_bearer_for(validator, new_identity_sub()),
        ).json()
    )

    assert body.included[0].aaa_tier is None


def test_preview_passes_min_percent_completed_through_to_spec():
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator)
    completion_floor = new_percent_completed()

    client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND, min_percent_completed=completion_floor),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert orchestrator.generate_calls[0][1].min_percent_completed == completion_floor


def test_preview_passes_sort_order_and_exclude_installed_on_through_to_spec():
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator)
    first_console_id, second_console_id = new_console_id(), new_console_id()
    excluded_console_ids = [first_console_id, second_console_id]

    client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND, sort_order=COMPOSITE_DESC, exclude_installed_on=excluded_console_ids),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    spec = orchestrator.generate_calls[0][1]
    assert spec.sort_order == COMPOSITE_DESC
    assert spec.exclude_installed_on == (first_console_id, second_console_id)


def test_preview_response_includes_percent_completed():
    candidate = _candidate(percent_completed=new_percent_completed())
    result = CollectionResult(included=(candidate,), excluded=(), used_gb=None)
    client, validator = _build(FakeOrchestrator(result=result))

    response = client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    served = CollectionPreviewResponse.model_validate(response.json()).included[0]
    assert served.percent_completed == candidate.percent_completed


def test_preview_never_resolves_trophy_completion_at_request_time():
    """A preview must not touch PSN. Completion is read from each candidate row.

    Before ``0015_library_entries_trophy_progress.sql`` this route fetched the caller's whole library and
    fuzzy-matched every game against their trophy titles on every request -- measured at ~38s for a
    411-game library, on the event loop. The percentage is now persisted, so the route hands the
    orchestrator nothing and the row's own value stands.
    """
    caller_sub = new_identity_sub()
    repository = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    _seed_link(repository, crypto, caller_sub, harvest_trophies=True)
    factory = FakeTrophyClientFactory()
    factory.linked[caller_sub] = FakeTrophyClient(titles=[])
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator, repository=repository, trophy_client_factory=factory)

    client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND),
        headers=_bearer_for(validator, caller_sub),
    )

    _, _, completion_map, completion_available = orchestrator.generate_calls[0]
    assert completion_map is None
    assert completion_available is False
    assert factory.calls == []


def test_preview_threads_the_completion_floor_through_to_the_spec():
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator)
    completion_floor = new_percent_completed()

    client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND, min_percent_completed=completion_floor),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    _, spec, _, _ = orchestrator.generate_calls[0]
    assert spec.min_percent_completed == completion_floor


def test_preview_parses_a_filter_predicate_onto_the_spec():
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator)
    predicate = Or(
        nodes=(
            GenreIn(values=(new_genre_name(),)),
            And(nodes=(GenreIn(values=(new_genre_name(),)), TierIn(values=(new_genre_name(),)))),
        )
    )

    response = client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND, filter_predicate=predicate_to_dict(predicate)),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.OK
    spec = orchestrator.generate_calls[0][1]
    assert spec.filter_predicate == predicate


def test_preview_rejects_a_malformed_filter_predicate_as_400_not_500():
    client, validator = _build()

    response = client.post(
        _path(client, collections_routes.preview_collection),
        json=_spec(kind=FILTER_LIST_KIND, filter_predicate={PREDICATE_OP_KEY: uuid.uuid4().hex}),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_save_definition_rejects_invalid_kind():
    client, validator = _build()

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=new_game_title(), kind=uuid.uuid4().hex),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST


def test_save_definition_persists_and_returns_it():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    name, genre = new_game_title(), new_genre_name()

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=name, kind=FILTER_LIST_KIND, genre_filter=[genre], min_score=new_review_score()),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.CREATED
    body = DefinitionResponse.model_validate(response.json())
    assert body.name == name
    assert body.genre_filter == [genre]
    assert len(collections_repository.definitions) == 1


def test_save_definition_rejects_a_console_the_caller_does_not_own():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    unowned_console_id = uuid.uuid4().hex

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=new_game_title(), kind=CAPACITY_FILL_KIND, console_id=unowned_console_id),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == collections_routes.unknown_console_detail(unowned_console_id)
    assert collections_repository.definitions == {}


def test_save_definition_rejects_an_exclude_installed_on_console_the_caller_does_not_own():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    unowned_console_id = uuid.uuid4().hex

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=new_game_title(), kind=FILTER_LIST_KIND, exclude_installed_on=[unowned_console_id]),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert response.json()["detail"] == collections_routes.unknown_excluded_consoles_detail([unowned_console_id])
    assert collections_repository.definitions == {}


def test_save_definition_round_trips_sort_order_and_exclude_installed_on():
    console_id = new_console_id()
    collections_repository = FakeCollectionsRepository(consoles=[_console(console_id)])
    client, validator = _build(collections_repository=collections_repository)

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(
            name=new_game_title(),
            kind=FILTER_LIST_KIND,
            sort_order=COMPOSITE_DESC,
            exclude_installed_on=[console_id],
        ),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.CREATED
    body = DefinitionResponse.model_validate(response.json())
    assert body.sort_order == COMPOSITE_DESC
    assert body.exclude_installed_on == [console_id]


def test_save_definition_accepts_a_console_the_caller_owns():
    console_id = new_console_id()
    collections_repository = FakeCollectionsRepository(consoles=[_console(console_id)])
    client, validator = _build(collections_repository=collections_repository)

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=new_game_title(), kind=CAPACITY_FILL_KIND, console_id=console_id),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.CREATED
    assert DefinitionResponse.model_validate(response.json()).console_id == console_id


def test_save_definition_persists_min_percent_completed():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    completion_floor = new_percent_completed()

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=new_game_title(), kind=FILTER_LIST_KIND, min_percent_completed=completion_floor),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.CREATED
    body = DefinitionResponse.model_validate(response.json())
    assert body.min_percent_completed == completion_floor
    assert collections_repository.definitions[body.definition_id].min_percent_completed == completion_floor


def test_save_definition_duplicate_name_returns_409():
    taken_name = new_game_title()
    collections_repository = FakeCollectionsRepository(duplicate_names={taken_name})
    client, validator = _build(collections_repository=collections_repository)

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=taken_name, kind=FILTER_LIST_KIND),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.CONFLICT
    assert taken_name in response.json()["detail"]


def test_list_definitions_scopes_to_caller():
    caller_sub = new_identity_sub()
    definition_a = _definition(identity_sub=caller_sub)
    definition_b = _definition()
    collections_repository = FakeCollectionsRepository([definition_a, definition_b])
    client, validator = _build(collections_repository=collections_repository)

    headers = _bearer_for(validator, caller_sub)

    response = client.get(_path(client, collections_routes.list_definitions), headers=headers)

    assert response.status_code == HTTPStatus.OK
    body = _DEFINITION_LIST.validate_python(response.json())
    assert len(body) == 1
    assert body[0].definition_id == definition_a.definition_id


def test_save_definition_stores_the_supplied_game_ids():
    first_game_id, second_game_id = new_game_id(), new_game_id()
    description = new_game_title()
    collections_repository = FakeCollectionsRepository(known_games={first_game_id, second_game_id})
    client, validator = _build(collections_repository=collections_repository)

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=new_game_title(), game_ids=[second_game_id, first_game_id], description=description),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.CREATED
    body = DefinitionResponse.model_validate(response.json())
    assert body.description == description
    assert collections_repository.items[body.definition_id] == (second_game_id, first_game_id)


def test_save_definition_defaults_kind_so_a_handpicked_list_needs_no_spec():
    game_id = new_game_id()
    collections_repository = FakeCollectionsRepository(known_games={game_id})
    client, validator = _build(collections_repository=collections_repository)

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=new_game_title(), game_ids=[game_id]),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.CREATED
    assert DefinitionResponse.model_validate(response.json()).kind == FILTER_LIST_KIND


def test_save_definition_persists_and_returns_a_filter_predicate():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)
    predicate = Or(nodes=(GenreIn(values=(new_genre_name(),)), ScoreAtLeast(threshold=new_review_score())))

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=new_game_title(), filter_predicate=predicate_to_dict(predicate)),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.CREATED
    body = DefinitionResponse.model_validate(response.json())
    assert body.filter_predicate == predicate_to_dict(predicate)
    saved = collections_repository.definitions[body.definition_id]
    assert saved.filter_predicate == predicate


def test_save_definition_rejects_a_malformed_filter_predicate_as_400_not_500():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=new_game_title(), filter_predicate={PREDICATE_OP_KEY: AND_OP, PREDICATE_NODES_KEY: []}),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert collections_repository.definitions == {}


def test_definition_with_no_filter_predicate_returns_null_not_an_empty_object():
    collections_repository = FakeCollectionsRepository()
    client, validator = _build(collections_repository=collections_repository)

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=new_game_title()),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert DefinitionResponse.model_validate(response.json()).filter_predicate is None


def test_save_definition_rejects_an_unknown_game_id():
    known_game_id, unknown_game_id = new_game_id(), new_game_id()
    collections_repository = FakeCollectionsRepository(known_games={known_game_id})
    client, validator = _build(collections_repository=collections_repository)

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=new_game_title(), game_ids=[known_game_id, unknown_game_id]),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert unknown_game_id in response.json()["detail"]
    assert collections_repository.definitions == {}


def test_save_definition_rejects_a_malformed_game_id_as_400_not_500():
    malformed_game_id = lowercase_token()
    collections_repository = FakeCollectionsRepository(malformed_game_ids={malformed_game_id})
    client, validator = _build(collections_repository=collections_repository)

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=new_game_title(), game_ids=[malformed_game_id]),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert "UUID" in response.json()["detail"]


def test_save_definition_lower_cases_game_ids_before_validating_them():
    game_id = new_game_id()
    collections_repository = FakeCollectionsRepository(known_games={game_id})
    client, validator = _build(collections_repository=collections_repository)

    response = client.post(
        _path(client, collections_routes.save_definition),
        json=_save(name=new_game_title(), game_ids=[game_id.upper()]),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.CREATED
    body = DefinitionResponse.model_validate(response.json())
    assert collections_repository.items[body.definition_id] == (game_id,)


def test_get_definition_returns_its_items():
    owner_sub = new_identity_sub()
    game_id = new_game_id()
    definition = _definition(identity_sub=owner_sub)
    collections_repository = FakeCollectionsRepository([definition], known_games={game_id})
    collections_repository.items[definition.definition_id] = (game_id,)
    client, validator = _build(collections_repository=collections_repository)

    response = client.get(
        _path(client, collections_routes.get_definition, definition_id=definition.definition_id),
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.OK
    body = DefinitionDetailResponse.model_validate(response.json())
    assert body.definition_id == definition.definition_id
    assert [item.game_id for item in body.items] == [game_id]
    assert body.items[0].cover_image_url == collections_repository.cover_image_url


def test_get_definition_items_returns_a_page_and_the_total():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub)
    offset = new_small_count()
    limit = new_small_count()
    game_ids = tuple(new_game_id() for _ in range(offset + limit + new_small_count()))
    collections_repository = FakeCollectionsRepository([definition], known_games=game_ids)
    collections_repository.items[definition.definition_id] = game_ids
    client, validator = _build(collections_repository=collections_repository)

    response = client.get(
        _path(client, collections_routes.get_definition_items, definition_id=definition.definition_id),
        params={LIMIT_PARAM: limit, OFFSET_PARAM: offset},
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.OK
    body = CollectionItemsPageResponse.model_validate(response.json())
    assert [item.game_id for item in body.items] == list(game_ids[offset : offset + limit])
    assert body.total == len(game_ids), "total counts the whole collection, not the page"


def test_get_definition_items_filters_by_title():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub)
    first_game_id, second_game_id = new_game_id(), new_game_id()
    collections_repository = FakeCollectionsRepository([definition], known_games={first_game_id, second_game_id})
    collections_repository.items[definition.definition_id] = (first_game_id, second_game_id)
    client, validator = _build(collections_repository=collections_repository)

    response = client.get(
        _path(client, collections_routes.get_definition_items, definition_id=definition.definition_id),
        params={"q": second_game_id},
        headers=_bearer_for(validator, owner_sub),
    )

    items = CollectionItemsPageResponse.model_validate(response.json()).items
    assert [item.game_id for item in items] == [second_game_id]


def test_get_definition_items_rejects_an_unknown_sort_field():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub)
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)

    response = client.get(
        _path(client, collections_routes.get_definition_items, definition_id=definition.definition_id),
        params={SORT_PARAM: lowercase_token()},
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY


def test_get_definition_items_not_owned_returns_404():
    definition = _definition()
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)

    response = client.get(
        _path(client, collections_routes.get_definition_items, definition_id=definition.definition_id),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_remove_definition_item_removes_only_that_title():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub)
    removed_game_id, kept_game_id = new_game_id(), new_game_id()
    collections_repository = FakeCollectionsRepository([definition], known_games={removed_game_id, kept_game_id})
    collections_repository.items[definition.definition_id] = (removed_game_id, kept_game_id)
    client, validator = _build(collections_repository=collections_repository)

    response = client.delete(
        _path(
            client,
            collections_routes.remove_definition_item,
            definition_id=definition.definition_id,
            game_id=removed_game_id,
        ),
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert collections_repository.items[definition.definition_id] == (kept_game_id,)


def test_remove_definition_item_not_a_member_returns_404():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub)
    member_game_id = new_game_id()
    collections_repository = FakeCollectionsRepository([definition], known_games={member_game_id})
    collections_repository.items[definition.definition_id] = (member_game_id,)
    client, validator = _build(collections_repository=collections_repository)

    response = client.delete(
        _path(
            client,
            collections_routes.remove_definition_item,
            definition_id=definition.definition_id,
            game_id=new_game_id(),
        ),
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_remove_definition_item_not_owned_returns_404_without_touching_the_collection():
    definition = _definition()
    member_game_id = new_game_id()
    collections_repository = FakeCollectionsRepository([definition], known_games={member_game_id})
    collections_repository.items[definition.definition_id] = (member_game_id,)
    client, validator = _build(collections_repository=collections_repository)

    response = client.delete(
        _path(
            client,
            collections_routes.remove_definition_item,
            definition_id=definition.definition_id,
            game_id=member_game_id,
        ),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert collections_repository.items[definition.definition_id] == (member_game_id,)


def test_get_definition_not_owned_returns_404():
    definition = _definition()
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)

    response = client.get(
        _path(client, collections_routes.get_definition, definition_id=definition.definition_id),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_patch_definition_renames_and_replaces_membership():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub)
    old_game_id, new_member_game_id = new_game_id(), new_game_id()
    new_name = new_game_title()
    collections_repository = FakeCollectionsRepository([definition], known_games={old_game_id, new_member_game_id})
    collections_repository.items[definition.definition_id] = (old_game_id,)
    client, validator = _build(collections_repository=collections_repository)

    response = client.patch(
        _path(client, collections_routes.update_definition, definition_id=definition.definition_id),
        json=_update(name=new_name, game_ids=[new_member_game_id]),
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.OK
    detail = DefinitionDetailResponse.model_validate(response.json())
    assert detail.name == new_name
    assert [item.game_id for item in detail.items] == [new_member_game_id]
    assert collections_repository.items[definition.definition_id] == (new_member_game_id,)


def test_patch_definition_leaves_omitted_fields_alone():
    owner_sub = new_identity_sub()
    original_description = new_game_title()
    definition = _definition(identity_sub=owner_sub, description=original_description)
    member_game_id = new_game_id()
    collections_repository = FakeCollectionsRepository([definition], known_games={member_game_id})
    collections_repository.items[definition.definition_id] = (member_game_id,)
    client, validator = _build(collections_repository=collections_repository)

    response = client.patch(
        _path(client, collections_routes.update_definition, definition_id=definition.definition_id),
        json=_update(name=new_game_title()),
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.OK
    assert DefinitionResponse.model_validate(response.json()).description == original_description
    assert collections_repository.items[definition.definition_id] == (member_game_id,)


def test_patch_definition_can_clear_a_description_with_an_explicit_null():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub, description=new_game_title())
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)

    response = client.patch(
        _path(client, collections_routes.update_definition, definition_id=definition.definition_id),
        json=_update(description=None),
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.OK
    assert DefinitionResponse.model_validate(response.json()).description is None


def test_patch_definition_duplicate_name_returns_409():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub)
    taken_name = new_game_title()
    collections_repository = FakeCollectionsRepository([definition], duplicate_names={taken_name})
    client, validator = _build(collections_repository=collections_repository)

    response = client.patch(
        _path(client, collections_routes.update_definition, definition_id=definition.definition_id),
        json=_update(name=taken_name),
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.CONFLICT


def test_patch_definition_not_owned_returns_404():
    definition = _definition()
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)

    response = client.patch(
        _path(client, collections_routes.update_definition, definition_id=definition.definition_id),
        json=_update(name=new_game_title()),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert collections_repository.definitions[definition.definition_id].name == definition.name


def test_delete_definition_removes_it():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub)
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)

    response = client.delete(
        _path(client, collections_routes.delete_definition, definition_id=definition.definition_id),
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert collections_repository.definitions == {}


def test_delete_definition_not_owned_returns_404():
    definition = _definition()
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)

    response = client.delete(
        _path(client, collections_routes.delete_definition, definition_id=definition.definition_id),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert definition.definition_id in collections_repository.definitions


def test_set_visibility_changes_it_and_returns_the_updated_definition():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub)
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)

    response = client.put(
        _path(client, collections_routes.set_visibility, definition_id=definition.definition_id),
        json=_visibility(VISIBILITY_PUBLIC),
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.OK
    assert DefinitionResponse.model_validate(response.json()).visibility == VISIBILITY_PUBLIC
    assert collections_repository.definitions[definition.definition_id].visibility == VISIBILITY_PUBLIC


def test_set_visibility_rejects_an_unknown_value():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub)
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)

    response = client.put(
        _path(client, collections_routes.set_visibility, definition_id=definition.definition_id),
        json=_visibility(uuid.uuid4().hex),
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert collections_repository.definitions[definition.definition_id].visibility == VISIBILITY_PRIVATE


def test_set_visibility_not_owned_returns_404():
    definition = _definition()
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)

    response = client.put(
        _path(client, collections_routes.set_visibility, definition_id=definition.definition_id),
        json=_visibility(VISIBILITY_PUBLIC),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_follow_a_public_collection():
    other = _definition(visibility=VISIBILITY_PUBLIC)
    collections_repository = FakeCollectionsRepository([other])
    client, validator = _build(collections_repository=collections_repository)
    follower_sub = new_identity_sub()

    response = client.post(
        _path(client, collections_routes.follow_definition, definition_id=other.definition_id),
        headers=_bearer_for(validator, follower_sub),
    )

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert collections_repository.collection_follows[other.definition_id] == {follower_sub}


def test_cannot_follow_your_own_collection():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub, visibility=VISIBILITY_PUBLIC)
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)

    response = client.post(
        _path(client, collections_routes.follow_definition, definition_id=definition.definition_id),
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.BAD_REQUEST
    assert collections_repository.collection_follows.get(definition.definition_id, set()) == set()


def test_cannot_follow_a_private_collection():
    other = _definition()
    collections_repository = FakeCollectionsRepository([other])
    client, validator = _build(collections_repository=collections_repository)

    response = client.post(
        _path(client, collections_routes.follow_definition, definition_id=other.definition_id),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert collections_repository.collection_follows.get(other.definition_id, set()) == set()


def test_follow_unknown_collection_is_404():
    client, validator = _build()

    response = client.post(
        _path(client, collections_routes.follow_definition, definition_id=new_definition_id()),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_unfollow_a_collection():
    other = _definition(visibility=VISIBILITY_PUBLIC)
    follower_sub = new_identity_sub()
    collections_repository = FakeCollectionsRepository([other])
    collections_repository.collection_follows[other.definition_id] = {follower_sub}
    client, validator = _build(collections_repository=collections_repository)

    response = client.delete(
        _path(client, collections_routes.unfollow_definition, definition_id=other.definition_id),
        headers=_bearer_for(validator, follower_sub),
    )

    assert response.status_code == HTTPStatus.NO_CONTENT
    assert collections_repository.collection_follows[other.definition_id] == set()


def test_unfollow_is_idempotent_even_for_an_unknown_collection():
    client, validator = _build()

    response = client.delete(
        _path(client, collections_routes.unfollow_definition, definition_id=new_definition_id()),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.NO_CONTENT


def test_lists_followed_collections():
    owner_sub, follower_sub = new_identity_sub(), new_identity_sub()
    followed = _definition(identity_sub=owner_sub, visibility=VISIBILITY_PUBLIC)
    not_followed = _definition(identity_sub=owner_sub, visibility=VISIBILITY_PUBLIC)
    collections_repository = FakeCollectionsRepository([followed, not_followed])
    collections_repository.collection_follows[followed.definition_id] = {follower_sub}
    client, validator = _build(collections_repository=collections_repository)

    headers = _bearer_for(validator, follower_sub)

    response = client.get(_path(client, collections_routes.list_followed_collections), headers=headers)

    assert response.status_code == HTTPStatus.OK
    assert [d.definition_id for d in _DEFINITION_LIST.validate_python(response.json())] == [followed.definition_id]


def test_run_definition_does_not_change_stored_membership():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub)
    member_game_id = new_game_id()
    collections_repository = FakeCollectionsRepository([definition])
    collections_repository.items[definition.definition_id] = (member_game_id,)
    orchestrator = FakeOrchestrator(result=CollectionResult(included=(_candidate(),), excluded=(), used_gb=None))
    client, validator = _build(orchestrator, collections_repository)

    response = client.post(
        _path(client, collections_routes.run_definition, definition_id=definition.definition_id),
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.CREATED
    assert collections_repository.items[definition.definition_id] == (member_game_id,)


def test_run_definition_does_not_resolve_completion_at_request_time():
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub)
    collections_repository = FakeCollectionsRepository([definition])
    orchestrator = FakeOrchestrator()
    client, validator = _build(orchestrator, collections_repository)

    client.post(
        _path(client, collections_routes.run_definition, definition_id=definition.definition_id),
        headers=_bearer_for(validator, owner_sub),
    )

    _, _, completion_map, completion_available = orchestrator.generate_calls[0]
    assert completion_map is None
    assert completion_available is False


def test_run_definition_not_found_returns_404():
    client, validator = _build()

    response = client.post(
        _path(client, collections_routes.run_definition, definition_id=new_definition_id()),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_run_definition_not_owned_returns_404():
    definition = _definition()
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(collections_repository=collections_repository)

    response = client.post(
        _path(client, collections_routes.run_definition, definition_id=definition.definition_id),
        headers=_bearer_for(validator, new_identity_sub()),
    )

    assert response.status_code == HTTPStatus.NOT_FOUND


def test_run_definition_generates_and_persists():
    owner_sub = new_identity_sub()
    genre = new_genre_name()
    definition = _definition(identity_sub=owner_sub, genre_filter=(genre,))
    candidate = _candidate()
    orchestrator = FakeOrchestrator(result=CollectionResult(included=(candidate,), excluded=(), used_gb=None))
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(orchestrator, collections_repository)

    response = client.post(
        _path(client, collections_routes.run_definition, definition_id=definition.definition_id),
        headers=_bearer_for(validator, owner_sub),
    )

    assert response.status_code == HTTPStatus.CREATED
    body = CollectionRunResponse.model_validate(response.json())
    assert body.run_id == collections_repository.run_id
    assert body.included[0].game_id == candidate.game_id
    assert len(collections_repository.saved_runs) == 1
    assert orchestrator.generate_calls[0][1].genre_filter == (genre,)


def test_run_caps_the_response_but_still_persists_every_result():
    """The cap is presentational. Truncating what `save_run` stores would silently discard run history,
    which is the opposite of what bounding the response body is for -- and would foreclose ever serving
    later pages from the stored run.
    """
    owner_sub = new_identity_sub()
    definition = _definition(identity_sub=owner_sub)
    limit = new_small_count()
    included = _candidates(limit + new_small_count())
    excluded = _candidates(limit + new_small_count())
    result = CollectionResult(included=included, excluded=excluded, used_gb=None)
    collections_repository = FakeCollectionsRepository([definition])
    client, validator = _build(FakeOrchestrator(result=result), collections_repository)

    body = CollectionRunResponse.model_validate(
        client.post(
            _path(client, collections_routes.run_definition, definition_id=definition.definition_id),
            params={LIMIT_PARAM: limit},
            headers=_bearer_for(validator, owner_sub),
        ).json()
    )

    assert [g.game_id for g in body.included] == [g.game_id for g in included[:limit]]
    assert body.included_total == len(included)
    assert body.excluded_total == len(excluded)

    _, _, _, saved_included, saved_excluded = collections_repository.saved_runs[0]
    assert len(saved_included) == len(included)
    assert len(saved_excluded) == len(excluded)
