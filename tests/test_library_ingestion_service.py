"""Tests for IngestionService, using hand-written fakes for LibraryClient and CatalogRepository."""

from __future__ import annotations

from curator.library.ingestion_service import IngestionService
from curator.psn.models import Entitlement


class FakeLibraryClient:
    def __init__(self, entitlements=None, raw_entries=None):
        self._entitlements = entitlements or []
        self._raw_entries = raw_entries or {
            (entitlement.entitlement_id or ""): {"id": entitlement.entitlement_id, "_fake_raw": True}
            for entitlement in self._entitlements
        }
        self.entitlements_calls: list[int] = []

    async def entitlements(self, limit=None):
        self.entitlements_calls.append(limit)
        return self._entitlements

    async def entitlements_with_raw(self, limit=None):
        self.entitlements_calls.append(limit)
        return [
            (entitlement, self._raw_entries.get(entitlement.entitlement_id or "", {}))
            for entitlement in self._entitlements
        ]


class FakeCatalogRepository:
    def __init__(self, pull_id="pull-1"):
        self._pull_id = pull_id
        self.record_pull_calls = []

    async def record_pull(self, identity_sub, source, snapshots, raw=None):
        self.record_pull_calls.append((identity_sub, source, snapshots, raw))
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


async def test_ingest_passes_verbatim_raw_payloads_through_to_record_pull():
    """Regression test: ingest() must hand PSN's raw entries to record_pull(raw=...).

    entitlement_snapshots.raw exists so a mapping bug can never permanently lose a field PSN sent, but
    ingest() used to call record_pull() without the raw argument, so every row stored '{}' -- discarding
    roughly two-thirds of the payload, including every artwork URL, unrecoverably.
    """
    entitlements = [Entitlement(entitlement_id="e1", game_meta_name="Bloodborne")]
    raw_entries = {"e1": {"id": "e1", "skuId": "sku-1", "titleMeta": {"imageUrl": "title.png"}}}
    library_client = FakeLibraryClient(entitlements=entitlements, raw_entries=raw_entries)
    repository = FakeCatalogRepository()
    service = IngestionService(library_client, repository)

    await service.ingest("sub-1")

    passed_raw = repository.record_pull_calls[0][3]
    assert passed_raw == raw_entries
    assert passed_raw["e1"]["titleMeta"]["imageUrl"] == "title.png"


async def test_ingest_maps_all_three_artwork_urls_and_sku_onto_the_snapshot():
    entitlements = [
        Entitlement(
            entitlement_id="e1",
            sku_id="sku-1",
            title_image_url="title.png",
            game_icon_url="icon.png",
            concept_icon_url="concept.png",
            is_game=True,
            platform_ids=("PS5",),
            active_date="2021-01-01T00:00:00Z",
        )
    ]
    service = IngestionService(FakeLibraryClient(entitlements=entitlements), FakeCatalogRepository())

    _pull_id, snapshots = await service.ingest("sub-1")

    assert snapshots[0].sku_id == "sku-1"
    assert snapshots[0].title_image_url == "title.png"
    assert snapshots[0].game_icon_url == "icon.png"
    assert snapshots[0].concept_icon_url == "concept.png"
    assert snapshots[0].is_game is True
    assert snapshots[0].platform_ids == ("PS5",)
    assert snapshots[0].active_date == "2021-01-01T00:00:00Z"


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


async def test_ingest_default_limit_is_unbounded():
    """Regression test: ingest() must default to limit=None (unbounded), not silently reintroduce a cap."""
    library_client = FakeLibraryClient(entitlements=[])
    service = IngestionService(library_client, FakeCatalogRepository())

    await service.ingest("sub-1")

    assert library_client.entitlements_calls == [None]


async def test_ingest_handles_missing_entitlement_id():
    entitlements = [Entitlement(entitlement_id=None, game_meta_name="Game")]
    service = IngestionService(FakeLibraryClient(entitlements=entitlements), FakeCatalogRepository())

    _, snapshots = await service.ingest("sub-1")

    assert snapshots[0].entitlement_id == ""
