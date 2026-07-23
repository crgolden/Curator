"""Tests for ``curator.app._enrichment_run_handler``, using hand-written fakes for every collaborator.

Covers the four passes ``POST /enrichment/runs`` now performs: OpenCritic cache refresh, catalog-wide
franchise reclassification, tier reclassification for already-enriched games, and best-effort
enrichment of still-unenriched games.
"""

from __future__ import annotations

from curator.app import _enrichment_run_handler
from curator.enrichment.enrichment_service import EnrichmentResult


class FakeEnrichmentService:
    def __init__(self, results_by_title=None):
        self._results_by_title = results_by_title or {}
        self.refresh_opencritic_cache_calls = 0
        self.enrich_calls: list[str] = []

    async def refresh_opencritic_cache(self):
        self.refresh_opencritic_cache_calls += 1
        return 0

    async def enrich_game(self, title, *, product_id, is_ps5, genre_priorities, publisher_tier_rules, size_estimates):
        self.enrich_calls.append(title)
        assert product_id is None
        result = self._results_by_title.get(title) or EnrichmentResult(
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
        return result, 0.0


class FakeCatalogRepository:
    def __init__(self, franchise_rules=None, all_games=None, size_estimates=None):
        self._franchise_rules = franchise_rules or []
        self._all_games = all_games or []
        self._size_estimates = size_estimates or []
        self.reclassify_franchise_calls = []
        self.get_size_estimates_calls = 0

    async def list_franchise_rules(self):
        return self._franchise_rules

    async def reclassify_franchise(self, rules):
        self.reclassify_franchise_calls.append(rules)
        return 0

    async def list_all_game_ids_and_titles(self):
        return self._all_games

    async def get_size_estimates(self):
        self.get_size_estimates_calls += 1
        return self._size_estimates


class FakeEnrichmentRepository:
    def __init__(self, publisher_tier_rules=None, unenriched=None, genre_rows=None):
        self._publisher_tier_rules = publisher_tier_rules or []
        self._unenriched = unenriched if unenriched is not None else []
        self._genre_rows = genre_rows or []
        self.reclassify_tier_calls = []
        self.get_active_genres_calls = 0
        self.save_calls = []

    async def list_publisher_tier_rules(self):
        return self._publisher_tier_rules

    async def reclassify_tier(self, rules):
        self.reclassify_tier_calls.append(rules)
        return 0

    async def get_unenriched_game_ids(self, game_ids):
        return self._unenriched

    async def get_active_genres(self):
        self.get_active_genres_calls += 1
        return self._genre_rows

    async def save_game_enrichment(self, game_id, genre_id, subgenre_id, result):
        self.save_calls.append((game_id, genre_id, subgenre_id, result))


async def test_handle_refreshes_opencritic_cache_first():
    enrichment_service = FakeEnrichmentService()
    handle = _enrichment_run_handler(enrichment_service, FakeCatalogRepository(), FakeEnrichmentRepository())

    await handle()

    assert enrichment_service.refresh_opencritic_cache_calls == 1


async def test_handle_reclassifies_franchise_for_every_game():
    rules = ["rule-1"]
    catalog_repository = FakeCatalogRepository(franchise_rules=rules)
    handle = _enrichment_run_handler(FakeEnrichmentService(), catalog_repository, FakeEnrichmentRepository())

    await handle()

    assert catalog_repository.reclassify_franchise_calls == [rules]


async def test_handle_reclassifies_tier_for_already_enriched_games():
    rules = ["rule-1"]
    enrichment_repository = FakeEnrichmentRepository(publisher_tier_rules=rules)
    handle = _enrichment_run_handler(FakeEnrichmentService(), FakeCatalogRepository(), enrichment_repository)

    await handle()

    assert enrichment_repository.reclassify_tier_calls == [rules]


async def test_handle_enriches_only_still_unenriched_games():
    catalog_repository = FakeCatalogRepository(all_games=[("id-1", "Title A"), ("id-2", "Title B")])
    enrichment_repository = FakeEnrichmentRepository(unenriched=["id-2"])
    enrichment_service = FakeEnrichmentService()
    handle = _enrichment_run_handler(enrichment_service, catalog_repository, enrichment_repository)

    await handle()

    assert enrichment_service.enrich_calls == ["Title B"]
    assert len(enrichment_repository.save_calls) == 1
    assert enrichment_repository.save_calls[0][0] == "id-2"


async def test_handle_skips_enrichment_pass_when_nothing_unenriched():
    catalog_repository = FakeCatalogRepository(all_games=[("id-1", "Title A")])
    enrichment_repository = FakeEnrichmentRepository(unenriched=[])
    enrichment_service = FakeEnrichmentService()
    handle = _enrichment_run_handler(enrichment_service, catalog_repository, enrichment_repository)

    await handle()

    assert enrichment_service.enrich_calls == []
    assert enrichment_repository.get_active_genres_calls == 0
    assert catalog_repository.get_size_estimates_calls == 0
