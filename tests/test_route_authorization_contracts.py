"""Tests for cross-cutting behavioral contracts that used to live only in route module docstrings:

* a collection run never writes console/storage-device install state (``curator.consoles_routes``,
  ``curator.storage_devices_routes``);
* deleting an enrichment key skips PSN re-verification, unlike unlinking PSN
  (``curator.enrichment_keys_routes``, ``curator.psn_routes``);
* moving a storage device between consoles touches only ``storage_devices`` in SQL, never
  ``storage_device_installs`` (``curator.collections.repository``).

Fixture conventions match the sibling route test files: hand-written fakes wired through
``create_app()``, no ``unittest.mock``. The storage-device-reattachment test asserts against the real
``CollectionsRepository`` SQL (via ``test_collections_repository``'s fake ``psycopg_pool`` objects) rather
than a route-level fake, because the route-level fake for that method never touches install rows regardless
of what the real SQL does.
"""

from __future__ import annotations

from random import randint
from uuid import uuid4

from fastapi.testclient import TestClient

from curator.app import create_app
from curator.collections.collection_orchestrator import CollectionResult
from curator.collections.game_candidate import GameCandidate
from curator.collections.repository import CollectionsRepository
from curator.persistence.crypto import TokenCrypto
from test_collections_repository import FakePool
from test_collections_routes import FakeCatalogRepository, FakeOrchestrator, _definition
from test_collections_routes import FakeCollectionsRepository as _RunCollectionsRepository
from test_enrichment_keys_routes import FakeEnrichmentKeysRepository
from test_routes import (
    NEW_IAT,
    OLD_IAT,
    FakeAgentFactory,
    FakeAuditRepository,
    FakeLibraryRepository,
    FakeRefreshSchedulesRepository,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _seed_link,
)


class _InstallTrackingCollectionsRepository(_RunCollectionsRepository):
    """Adds console/storage-device install-state recording to the collections-run fake, so a run's
    handler can be asserted to never touch either -- an auto-carry regression would show up as a call
    recorded here.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.set_console_install_calls: list[tuple[str, str, bool]] = []
        self.set_storage_device_install_calls: list[tuple[str, str, bool]] = []

    async def set_console_install(self, console_id, game_id, installed):
        self.set_console_install_calls.append((console_id, game_id, installed))

    async def set_storage_device_install(self, device_id, game_id, installed):
        self.set_storage_device_install_calls.append((device_id, game_id, installed))


def test_a_collection_run_never_writes_console_or_storage_device_install_state():
    definition = _definition(definition_id=f"def-{uuid4()}", identity_sub=f"sub-{uuid4()}")
    candidate = GameCandidate(
        game_id=f"game-{uuid4()}",
        title=f"Generated Title {uuid4()}",
        genre="Action",
        aaa_tier="AAA",
        franchise=f"Generated Franchise {uuid4()}",
        composite_score=float(randint(50, 100)),
        rank_score=randint(1, 5),
        size_gb=float(randint(10, 100)),
    )
    orchestrator = FakeOrchestrator(
        result=CollectionResult(included=(candidate,), excluded=(), used_gb=candidate.size_gb)
    )
    collections_repository = _InstallTrackingCollectionsRepository(definitions=[definition])

    repository = FakeRepository()
    token_crypto = TokenCrypto(TokenCrypto.generate_key())
    validator = FakeTokenValidator()
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=validator,
        catalog_repository=FakeCatalogRepository(),
    )
    app.state.collection_orchestrator = orchestrator
    app.state.collections_repository = collections_repository
    client = TestClient(app)
    validator.register("token-a", _claims(sub=definition.identity_sub))

    response = client.post(f"/collections/{definition.definition_id}/runs", headers=_bearer("token-a"))

    assert response.status_code == 201
    assert collections_repository.set_console_install_calls == []
    assert collections_repository.set_storage_device_install_calls == []


async def test_the_install_tracking_fake_records_calls_unlike_a_collection_run():
    collections_repository = _InstallTrackingCollectionsRepository(definitions=[])
    console_id, device_id, game_id = f"console-{uuid4()}", f"device-{uuid4()}", f"game-{uuid4()}"

    await collections_repository.set_console_install(console_id, game_id, True)
    await collections_repository.set_storage_device_install(device_id, game_id, True)

    assert collections_repository.set_console_install_calls == [(console_id, game_id, True)], (
        "the collection-run test asserts these are empty, which would pass against a fake that recorded nothing at all"
    )
    assert collections_repository.set_storage_device_install_calls == [(device_id, game_id, True)]


def test_deleting_an_enrichment_key_does_not_reverify_the_psn_link():
    sub = f"sub-{uuid4()}"
    repo = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    agent_factory = FakeAgentFactory(repo, crypto)
    _seed_link(repo, crypto, sub, last_verified_at=OLD_IAT)
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(sub=sub, iat=NEW_IAT))
    app = create_app(
        _make_settings(),
        repository=repo,
        token_crypto=crypto,
        agent_factory=agent_factory,
        token_validator=validator,
        enrichment_keys_repository=FakeEnrichmentKeysRepository(),
        audit_repository=FakeAuditRepository(),
    )
    client = TestClient(app)

    response = client.delete("/me/enrichment-keys/rawg", headers=_bearer("valid-token"))

    assert response.status_code == 204
    assert agent_factory.calls == []


def test_unlinking_psn_does_reverify_the_link_unlike_deleting_a_key():
    """The discriminating control for the test above: without it, asserting ``calls == []`` for the key
    delete would pass vacuously even with the assertion backwards."""
    sub = f"sub-{uuid4()}"
    repo = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    agent_factory = FakeAgentFactory(repo, crypto)
    _seed_link(repo, crypto, sub, last_verified_at=OLD_IAT)
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(sub=sub, iat=NEW_IAT))
    app = create_app(
        _make_settings(),
        repository=repo,
        token_crypto=crypto,
        agent_factory=agent_factory,
        token_validator=validator,
        library_repository=FakeLibraryRepository(),
        refresh_schedules_repository=FakeRefreshSchedulesRepository(),
        audit_repository=FakeAuditRepository(),
    )
    client = TestClient(app)

    response = client.delete("/psn/link", headers=_bearer("valid-token"))

    assert response.status_code == 204
    assert agent_factory.calls == [(sub, None)]


async def test_moving_a_storage_device_between_consoles_touches_only_the_storage_devices_table():
    identity_sub = f"sub-{uuid4()}"
    device_id = f"device-{uuid4()}"
    console_id = f"console-{uuid4()}"
    capacity_gb = float(randint(100, 2000))
    pool = FakePool(fetchone_results=[(device_id, console_id, "Drive", "usb", capacity_gb, 0.0)])
    repo = CollectionsRepository(pool)

    device = await repo.set_storage_device_attachment(identity_sub, device_id, console_id)

    assert device is not None
    assert device.console_id == console_id
    assert len(pool.connections) == 2, "one connection for the UPDATE, one for the follow-up read"
    executed_sql = [sql for conn in pool.connections for sql, _params in conn.executed]
    assert len(executed_sql) == 2
    assert "UPDATE storage_devices" in executed_sql[0]
    assert "FROM storage_devices" in executed_sql[1]
    for sql in executed_sql:
        assert "storage_device_installs" not in sql
