"""Tests for POST /collections/preview, using create_app() with fake CatalogRepository/CollectionOrchestrator."""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.collections.collection_orchestrator import CollectionResult
from curator.collections.game_candidate import GameCandidate
from curator.collections.repository import CollectionDefinition
from curator.persistence.crypto import TokenCrypto
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings


class FakeCatalogRepository:
    async def get_size_estimates(self):
        return []


class FakeOrchestrator:
    def __init__(self, result=None, raises=None):
        self._result = result or CollectionResult(included=(), excluded=(), used_gb=None)
        self._raises = raises
        self.generate_calls = []

    async def generate(self, identity_sub, spec, *, size_estimates):
        self.generate_calls.append((identity_sub, spec))
        if self._raises:
            raise self._raises
        return self._result


class FakeCollectionsRepository:
    def __init__(self, definitions=None):
        self.definitions: dict[str, CollectionDefinition] = {d.definition_id: d for d in (definitions or [])}
        self.saved_runs: list[tuple] = []
        self._next_id = 1

    async def save_definition(self, identity_sub, name, spec):
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
        )
        return definition_id

    async def list_definitions(self, identity_sub):
        return [d for d in self.definitions.values() if d.identity_sub == identity_sub]

    async def get_definition(self, identity_sub, definition_id):
        definition = self.definitions.get(definition_id)
        if definition is None or definition.identity_sub != identity_sub:
            return None
        return definition

    async def save_run(self, identity_sub, definition_id, spec_snapshot, included, excluded):
        self.saved_runs.append((identity_sub, definition_id, spec_snapshot, included, excluded))
        return "run-1"


def _build(orchestrator=None, collections_repository=None):
    repository = FakeRepository()
    token_crypto = TokenCrypto(Fernet.generate_key())
    validator = FakeTokenValidator()
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=validator,
        catalog_repository=FakeCatalogRepository(),
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
