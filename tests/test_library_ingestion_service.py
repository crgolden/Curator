"""Tests for IngestionService, using hand-written fakes for LibraryClient and CatalogRepository."""

from __future__ import annotations

from curator.library.ingestion_service import IngestionService
from curator.psn.models import Entitlement


class FakeLibraryClient:
    def __init__(self, entitlements=None):
        self._entitlements = entitlements or []
        self.entitlements_calls: list[int] = []

    async def entitlements(self, limit=500):
        self.entitlements_calls.append(limit)
        return self._entitlements


class FakeCatalogRepository:
    def __init__(self, pull_id="pull-1"):
        self._pull_id = pull_id
        self.record_pull_calls = []

    async def record_pull(self, identity_sub, source, snapshots):
        self.record_pull_calls.append((identity_sub, source, snapshots))
        return self._pull_id


async def test_ingest_converts_entitlements_to_snapshots_and_records_pull():
    entitlements = [
        Entitlement(
            entitlement_id="e1",
            concept_id="c1",
            product_id="p1",
            title_id="t1",
            game_meta_name="Bloodborne",
            concept_meta_name=None,
            title_meta_name="Bloodborne",
            package_type="PS4GD",
            active=True,
        )
    ]
    library_client = FakeLibraryClient(entitlements=entitlements)
    repository = FakeCatalogRepository(pull_id="pull-42")
    service = IngestionService(library_client, repository)

    pull_id, snapshots = await service.ingest("sub-1")

    assert pull_id == "pull-42"
    assert len(snapshots) == 1
    assert snapshots[0].entitlement_id == "e1"
    assert snapshots[0].concept_id == "c1"
    assert snapshots[0].game_meta_name == "Bloodborne"
    assert snapshots[0].title_meta_name == "Bloodborne"
    assert snapshots[0].package_type == "PS4GD"
    assert snapshots[0].active is True
    assert repository.record_pull_calls[0][0] == "sub-1"
    assert repository.record_pull_calls[0][1] == "curator-live"


async def test_ingest_preserves_distinct_game_and_title_meta_names():
    entitlements = [
        Entitlement(
            entitlement_id="e1",
            game_meta_name="CoffeeTalk",
            title_meta_name="Coffee Talk",
        )
    ]
    service = IngestionService(FakeLibraryClient(entitlements=entitlements), FakeCatalogRepository())

    _, snapshots = await service.ingest("sub-1")

    assert snapshots[0].game_meta_name == "CoffeeTalk"
    assert snapshots[0].title_meta_name == "Coffee Talk"


async def test_ingest_respects_limit():
    library_client = FakeLibraryClient(entitlements=[])
    service = IngestionService(library_client, FakeCatalogRepository())

    await service.ingest("sub-1", limit=100)

    assert library_client.entitlements_calls == [100]


async def test_ingest_handles_missing_entitlement_id():
    entitlements = [Entitlement(entitlement_id=None, game_meta_name="Game")]
    service = IngestionService(FakeLibraryClient(entitlements=entitlements), FakeCatalogRepository())

    _, snapshots = await service.ingest("sub-1")

    assert snapshots[0].entitlement_id == ""
