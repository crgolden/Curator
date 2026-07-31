"""Tests for ``curator.app._enrichment_run_handler``, using hand-written fakes for every collaborator.

Covers the four passes ``POST /enrichment/runs`` now performs: OpenCritic cache refresh, catalog-wide
franchise reclassification, tier reclassification for already-enriched games, and best-effort
enrichment of still-unenriched games -- plus the refactor this file was extended for: a provider being
unconfigured/auth-rejected/rate-limited never fails the whole job (it's recorded in the returned result
summary instead), and passes 2/3 are skipped when their driving rule set hasn't changed since the last
run that actually executed them.
"""

from __future__ import annotations

from curator.app import _enrichment_run_handler
from curator.catalog.franchise_assigner import FranchiseRule, fingerprint_franchise_rules
from curator.enrichment.enrichment_service import EnrichmentAuthError, EnrichmentRateLimitError, EnrichmentResult
from curator.enrichment.publisher_tier import PublisherTierRule, fingerprint_publisher_tier_rules


class FakeEnrichmentService:
    def __init__(
        self,
        results_by_title=None,
        *,
        has_rawg_client=True,
        has_opencritic_client=True,
        refresh_opencritic_cache_error=None,
        enrich_game_error=None,
        enrich_game_error_at_call=0,
    ):
        self._results_by_title = results_by_title or {}
        self.refresh_opencritic_cache_calls = 0
        self.enrich_calls: list[str] = []
        self.has_rawg_client = has_rawg_client
        self.has_opencritic_client = has_opencritic_client
        self._refresh_opencritic_cache_error = refresh_opencritic_cache_error
        self._enrich_game_error = enrich_game_error
        self._enrich_game_error_at_call = enrich_game_error_at_call

    async def refresh_opencritic_cache(self):
        self.refresh_opencritic_cache_calls += 1
        if self._refresh_opencritic_cache_error is not None:
            raise self._refresh_opencritic_cache_error
        return 0

    async def enrich_game(self, title, *, product_id, is_ps5, genre_priorities, publisher_tier_rules, size_estimates):
        assert product_id is None
        call_index = len(self.enrich_calls)
        self.enrich_calls.append(title)
        if self._enrich_game_error is not None and call_index == self._enrich_game_error_at_call:
            raise self._enrich_game_error
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
    def __init__(self, franchise_rules=None, all_games=None, size_estimates=None, franchise_rules_fingerprint=None):
        self._franchise_rules = franchise_rules or []
        self._all_games = all_games or []
        self._size_estimates = size_estimates or []
        self._franchise_rules_fingerprint = franchise_rules_fingerprint
        self.reclassify_franchise_calls = []
        self.set_franchise_rules_fingerprint_calls = []
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

    async def get_franchise_rules_fingerprint(self):
        return self._franchise_rules_fingerprint

    async def set_franchise_rules_fingerprint(self, fingerprint):
        self._franchise_rules_fingerprint = fingerprint
        self.set_franchise_rules_fingerprint_calls.append(fingerprint)


class FakeEnrichmentRepository:
    def __init__(
        self,
        publisher_tier_rules=None,
        unenriched=None,
        genre_rows=None,
        publisher_tier_rules_fingerprint=None,
    ):
        self._publisher_tier_rules = publisher_tier_rules or []
        self._unenriched = unenriched if unenriched is not None else []
        self._genre_rows = genre_rows or []
        self._publisher_tier_rules_fingerprint = publisher_tier_rules_fingerprint
        self.reclassify_tier_calls = []
        self.set_publisher_tier_rules_fingerprint_calls = []
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

    async def get_publisher_tier_rules_fingerprint(self):
        return self._publisher_tier_rules_fingerprint

    async def set_publisher_tier_rules_fingerprint(self, fingerprint):
        self._publisher_tier_rules_fingerprint = fingerprint
        self.set_publisher_tier_rules_fingerprint_calls.append(fingerprint)


_FRANCHISE_RULES = [FranchiseRule(rule_id="r1", pattern="halo", franchise="Halo", priority=0)]
_TIER_RULES = [PublisherTierRule(tier_id="t1", pattern="sony", tier="AAA", match_kind="substring")]


async def test_handle_refreshes_opencritic_cache_first():
    enrichment_service = FakeEnrichmentService()
    handle = _enrichment_run_handler(enrichment_service, FakeCatalogRepository(), FakeEnrichmentRepository())

    await handle()

    assert enrichment_service.refresh_opencritic_cache_calls == 1


async def test_handle_reclassifies_franchise_for_every_game():
    catalog_repository = FakeCatalogRepository(franchise_rules=_FRANCHISE_RULES)
    handle = _enrichment_run_handler(FakeEnrichmentService(), catalog_repository, FakeEnrichmentRepository())

    await handle()

    assert catalog_repository.reclassify_franchise_calls == [_FRANCHISE_RULES]


async def test_handle_reclassifies_tier_for_already_enriched_games():
    enrichment_repository = FakeEnrichmentRepository(publisher_tier_rules=_TIER_RULES)
    handle = _enrichment_run_handler(FakeEnrichmentService(), FakeCatalogRepository(), enrichment_repository)

    await handle()

    assert enrichment_repository.reclassify_tier_calls == [_TIER_RULES]


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


async def test_handle_skips_opencritic_refresh_when_not_configured():
    enrichment_service = FakeEnrichmentService(has_opencritic_client=False)
    catalog_repository = FakeCatalogRepository(franchise_rules=_FRANCHISE_RULES)
    handle = _enrichment_run_handler(enrichment_service, catalog_repository, FakeEnrichmentRepository())

    result = await handle()

    assert result is not None
    assert enrichment_service.refresh_opencritic_cache_calls == 0
    assert result["opencritic_cache_refresh"] == {"status": "not_configured"}
    # passes 2/3/4 still ran despite OpenCritic being unconfigured
    assert catalog_repository.reclassify_franchise_calls == [_FRANCHISE_RULES]


async def test_handle_records_opencritic_auth_error_without_failing_job():
    enrichment_service = FakeEnrichmentService(
        refresh_opencritic_cache_error=EnrichmentAuthError("opencritic", "bad key")
    )
    catalog_repository = FakeCatalogRepository(franchise_rules=_FRANCHISE_RULES)
    handle = _enrichment_run_handler(enrichment_service, catalog_repository, FakeEnrichmentRepository())

    result = await handle()

    assert result is not None
    assert result["opencritic_cache_refresh"] == {"status": "auth_error", "detail": "bad key"}
    # the job as a whole still completes normally -- passes 2/3/4 still ran
    assert catalog_repository.reclassify_franchise_calls == [_FRANCHISE_RULES]


async def test_handle_records_opencritic_rate_limit_without_failing_job():
    enrichment_service = FakeEnrichmentService(
        refresh_opencritic_cache_error=EnrichmentRateLimitError("opencritic", 3600.0)
    )
    handle = _enrichment_run_handler(enrichment_service, FakeCatalogRepository(), FakeEnrichmentRepository())

    result = await handle()

    assert result is not None
    assert result["opencritic_cache_refresh"] == {"status": "rate_limited", "retry_after_seconds": 3600.0}


async def test_handle_skips_franchise_reclassification_when_rules_unchanged():
    fingerprint = fingerprint_franchise_rules(_FRANCHISE_RULES)
    catalog_repository = FakeCatalogRepository(
        franchise_rules=_FRANCHISE_RULES, franchise_rules_fingerprint=fingerprint
    )
    handle = _enrichment_run_handler(FakeEnrichmentService(), catalog_repository, FakeEnrichmentRepository())

    result = await handle()

    assert result is not None
    assert catalog_repository.reclassify_franchise_calls == []
    assert result["franchise_reclassification"] == {"status": "skipped_unchanged"}


async def test_handle_runs_franchise_reclassification_when_rules_changed():
    catalog_repository = FakeCatalogRepository(
        franchise_rules=_FRANCHISE_RULES, franchise_rules_fingerprint="stale-fingerprint"
    )
    handle = _enrichment_run_handler(FakeEnrichmentService(), catalog_repository, FakeEnrichmentRepository())

    result = await handle()

    assert result is not None
    assert catalog_repository.reclassify_franchise_calls == [_FRANCHISE_RULES]
    assert result["franchise_reclassification"]["status"] == "ran"
    assert catalog_repository.set_franchise_rules_fingerprint_calls == [fingerprint_franchise_rules(_FRANCHISE_RULES)]


async def test_handle_skips_tier_reclassification_when_rules_unchanged():
    fingerprint = fingerprint_publisher_tier_rules(_TIER_RULES)
    enrichment_repository = FakeEnrichmentRepository(
        publisher_tier_rules=_TIER_RULES, publisher_tier_rules_fingerprint=fingerprint
    )
    handle = _enrichment_run_handler(FakeEnrichmentService(), FakeCatalogRepository(), enrichment_repository)

    result = await handle()

    assert result is not None
    assert enrichment_repository.reclassify_tier_calls == []
    assert result["tier_reclassification"] == {"status": "skipped_unchanged"}


async def test_handle_runs_tier_reclassification_when_rules_changed():
    enrichment_repository = FakeEnrichmentRepository(
        publisher_tier_rules=_TIER_RULES, publisher_tier_rules_fingerprint="stale-fingerprint"
    )
    handle = _enrichment_run_handler(FakeEnrichmentService(), FakeCatalogRepository(), enrichment_repository)

    result = await handle()

    assert result is not None
    assert enrichment_repository.reclassify_tier_calls == [_TIER_RULES]
    assert result["tier_reclassification"]["status"] == "ran"
    assert enrichment_repository.set_publisher_tier_rules_fingerprint_calls == [
        fingerprint_publisher_tier_rules(_TIER_RULES)
    ]


async def test_handle_stops_enrichment_pass_on_provider_error_but_reports_partial_progress():
    catalog_repository = FakeCatalogRepository(all_games=[("id-1", "Title A"), ("id-2", "Title B")])
    enrichment_repository = FakeEnrichmentRepository(unenriched=["id-1", "id-2"])
    enrichment_service = FakeEnrichmentService(
        enrich_game_error=EnrichmentAuthError("rawg", "bad key"), enrich_game_error_at_call=1
    )
    handle = _enrichment_run_handler(enrichment_service, catalog_repository, enrichment_repository)

    result = await handle()

    assert result is not None
    # the first game was enriched and saved before the stop
    assert len(enrichment_repository.save_calls) == 1
    assert enrichment_repository.save_calls[0][0] == "id-1"
    enrichment_summary = result["enrichment"]
    assert enrichment_summary["enriched_count"] == 1
    assert enrichment_summary["remaining_count"] == 1
    assert enrichment_summary["stopped_provider"] == "rawg"
    assert enrichment_summary["stopped_reason"] == "auth_error"


async def test_handle_reports_psn_as_not_configured():
    handle = _enrichment_run_handler(FakeEnrichmentService(), FakeCatalogRepository(), FakeEnrichmentRepository())

    result = await handle()

    assert result is not None
    assert result["enrichment"]["providers"]["psn"] == "not_configured"


async def test_handle_reports_rawg_and_opencritic_provider_availability():
    enrichment_service = FakeEnrichmentService(has_rawg_client=False, has_opencritic_client=True)
    handle = _enrichment_run_handler(enrichment_service, FakeCatalogRepository(), FakeEnrichmentRepository())

    result = await handle()

    assert result is not None
    assert result["enrichment"]["providers"]["rawg"] == "not_configured"
    assert result["enrichment"]["providers"]["opencritic"] == "ok"
