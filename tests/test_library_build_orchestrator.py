"""Tests for LibraryBuildOrchestrator, using hand-written fakes for every collaborator.

``canonicalize_current_entitlements()``/``build()`` exercise the REAL
``curator.catalog.canonicalization_service.canonicalize`` (already covered by its own dedicated test
suite) end-to-end against fake repositories/clients, rather than faking canonicalization itself.
"""

from __future__ import annotations

from curator.catalog.canonicalization_service import EntitlementSnapshot
from curator.enrichment.enrichment_service import EnrichmentResult
from curator.library.library_build_orchestrator import LibraryBuildOrchestrator


class FakeIngestionService:
    def __init__(self, pull_id="pull-1", snapshots=None):
        self._pull_id = pull_id
        self._snapshots = snapshots or []
        self.ingest_calls: list[tuple[str, int]] = []

    async def ingest(self, identity_sub, limit=500):
        self.ingest_calls.append((identity_sub, limit))
        return self._pull_id, self._snapshots


class FakeCatalogRepository:
    def __init__(self):
        self.upsert_calls = []
        self._next_id = 0

    async def list_exclusion_rules(self):
        return []

    async def list_franchise_rules(self):
        return []

    async def get_edition_ranks(self):
        return {}

    async def get_name_overrides(self):
        return {}

    async def get_globally_excluded_concept_ids(self):
        return set()

    async def upsert_game(self, game):
        self._next_id += 1
        game_id = f"game-{self._next_id}"
        self.upsert_calls.append((game_id, game))
        return game_id


class FakeEnrichmentService:
    def __init__(self, result=None, size=59.0):
        self._result = result or EnrichmentResult(
            genre="Action",
            subgenre="",
            release_year=None,
            developer=None,
            publisher=None,
            esrb=None,
            multiplayer=None,
            critical_score=None,
            oc_score=None,
            oc_tier=None,
            oc_percent_recommended=None,
            psn_rating=None,
            score_source=None,
            aaa_tier="Indie",
            rawg_enriched=False,
            opencritic_enriched=False,
        )
        self._size = size
        self.enrich_calls: list[str] = []
        self.opencritic_topup_incomplete = False

    async def enrich_game(self, title, *, product_id, is_ps5, genre_priorities, publisher_tier_rules, size_estimates):
        self.enrich_calls.append(title)
        return self._result, self._size


class FakeEnrichmentRepository:
    def __init__(self, genre_rows=None, unenriched_override=None):
        self._genre_rows = genre_rows or [("genre-id-1", "Action", 0)]
        self._unenriched_override = unenriched_override
        self.save_calls = []

    async def get_unenriched_game_ids(self, game_ids):
        if self._unenriched_override is not None:
            return self._unenriched_override
        return list(game_ids)

    async def get_active_genres(self):
        return self._genre_rows

    async def save_game_enrichment(self, game_id, genre_id, subgenre_id, result):
        self.save_calls.append((game_id, genre_id, subgenre_id, result))


class FakeLibraryRepository:
    def __init__(self):
        self.upsert_calls = []

    async def upsert_entry(
        self, identity_sub, game_id, *, native_ps5, ps4_eligible, owned_edition, winning_entitlement_id, product_id
    ):
        self.upsert_calls.append((identity_sub, game_id, native_ps5, ps4_eligible))


def _snapshot(title="God of War", concept_id="c1", entitlement_id="e1", package_type="PS4GD"):
    return EntitlementSnapshot(
        entitlement_id=entitlement_id,
        concept_id=concept_id,
        product_id="p1",
        title_id="t1",
        game_meta_name=title,
        concept_meta_name=None,
        title_meta_name=title,
        package_type=package_type,
        active=None,
    )


def _orchestrator(
    ingestion_service=None,
    catalog_repository=None,
    enrichment_service=None,
    enrichment_repository=None,
    library_repository=None,
):
    return LibraryBuildOrchestrator(
        ingestion_service=ingestion_service or FakeIngestionService(),
        catalog_repository=catalog_repository or FakeCatalogRepository(),
        enrichment_service=enrichment_service or FakeEnrichmentService(),
        enrichment_repository=enrichment_repository or FakeEnrichmentRepository(),
        library_repository=library_repository or FakeLibraryRepository(),
    )


async def test_ingest_delegates_to_ingestion_service():
    ingestion_service = FakeIngestionService(pull_id="pull-99")
    orchestrator = _orchestrator(ingestion_service=ingestion_service)

    pull_id = await orchestrator.ingest("sub-1")

    assert pull_id == "pull-99"
    assert ingestion_service.ingest_calls == [("sub-1", 500)]


async def test_canonicalize_current_entitlements_runs_real_canonicalization():
    ingestion_service = FakeIngestionService(snapshots=[_snapshot()])
    orchestrator = _orchestrator(ingestion_service=ingestion_service)

    games = await orchestrator.canonicalize_current_entitlements("sub-1")

    assert len(games) == 1
    assert games[0].canonical_title == "God of War"


async def test_persist_and_link_upserts_game_and_library_entry():
    catalog_repository = FakeCatalogRepository()
    library_repository = FakeLibraryRepository()
    ingestion_service = FakeIngestionService(snapshots=[_snapshot()])
    orchestrator = _orchestrator(
        ingestion_service=ingestion_service,
        catalog_repository=catalog_repository,
        library_repository=library_repository,
    )
    games = await orchestrator.canonicalize_current_entitlements("sub-1")

    game_ids = await orchestrator.persist_and_link("sub-1", games)

    assert game_ids == ["game-1"]
    assert catalog_repository.upsert_calls[0][0] == "game-1"
    assert library_repository.upsert_calls == [("sub-1", "game-1", games[0].native_ps5, games[0].ps4_eligible)]


async def test_enrich_delta_only_enriches_unenriched_games():
    enrichment_service = FakeEnrichmentService()
    enrichment_repository = FakeEnrichmentRepository(unenriched_override=["game-2"])
    orchestrator = _orchestrator(enrichment_service=enrichment_service, enrichment_repository=enrichment_repository)
    games = [_fake_canonical("Game A"), _fake_canonical("Game B")]

    result = await orchestrator.enrich_delta(games, ["game-1", "game-2"], publisher_tier_rules=[], size_estimates=[])

    assert result.enriched_count == 1
    assert enrichment_service.enrich_calls == ["Game B"]
    assert enrichment_repository.save_calls[0][0] == "game-2"


async def test_enrich_delta_tracks_newly_enriched_titles_per_provider():
    from curator.enrichment.enrichment_service import EnrichmentResult

    result_both = EnrichmentResult(
        genre="Action",
        subgenre="",
        release_year=None,
        developer=None,
        publisher=None,
        esrb=None,
        multiplayer=None,
        critical_score=None,
        oc_score=None,
        oc_tier=None,
        oc_percent_recommended=None,
        psn_rating=None,
        score_source=None,
        aaa_tier="Indie",
        rawg_enriched=True,
        opencritic_enriched=False,
    )
    enrichment_service = FakeEnrichmentService(result=result_both)
    orchestrator = _orchestrator(enrichment_service=enrichment_service)
    games = [_fake_canonical("Game A")]

    result = await orchestrator.enrich_delta(games, ["game-1"], publisher_tier_rules=[], size_estimates=[])

    assert result.rawg_enriched_titles == ["Game A"]
    assert result.opencritic_enriched_titles == []


async def test_enrich_delta_resolves_genre_id_from_active_genres():
    enrichment_service = FakeEnrichmentService()
    enrichment_repository = FakeEnrichmentRepository(genre_rows=[("genre-id-1", "Action", 0)])
    orchestrator = _orchestrator(enrichment_service=enrichment_service, enrichment_repository=enrichment_repository)
    games = [_fake_canonical("Game A")]

    await orchestrator.enrich_delta(games, ["game-1"], publisher_tier_rules=[], size_estimates=[])

    _saved_game_id, genre_id, subgenre_id, _result = enrichment_repository.save_calls[0]
    assert genre_id == "genre-id-1"
    assert subgenre_id is None


async def test_build_runs_full_pipeline_end_to_end():
    ingestion_service = FakeIngestionService(pull_id="pull-1", snapshots=[_snapshot()])
    orchestrator = _orchestrator(ingestion_service=ingestion_service)

    result = await orchestrator.build("sub-1", publisher_tier_rules=[], size_estimates=[])

    assert result.pull_id == "pull-1"
    assert result.games_canonicalized == 1
    assert result.games_enriched == 1
    assert result.rawg_enriched_titles == []
    assert result.opencritic_enriched_titles == []
    assert result.opencritic_topup_incomplete is False


def _fake_canonical(title):
    from curator.catalog.canonicalization_service import CanonicalGame

    return CanonicalGame(
        canonical_title=title,
        native_ps5=False,
        ps4_eligible=True,
        franchise="",
        product_id="p1",
        concept_ids=("c1",),
        winning_entitlement_id="e1",
    )
