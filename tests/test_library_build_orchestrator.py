"""Tests for LibraryBuildOrchestrator, using hand-written fakes for every collaborator.

``canonicalize_current_entitlements()``/``build()`` exercise the REAL
``curator.catalog.canonicalization_service.canonicalize`` (already covered by its own dedicated test
suite) end-to-end against fake repositories/clients, rather than faking canonicalization itself.
"""

from __future__ import annotations

from curator.catalog.canonicalization_service import EntitlementSnapshot
from curator.enrichment.enrichment_service import EnrichmentAuthError, EnrichmentRateLimitError, EnrichmentResult
from curator.library.library_build_orchestrator import LibraryBuildOrchestrator


class FakeIngestionService:
    def __init__(self, pull_id="pull-1", snapshots=None):
        self._pull_id = pull_id
        self._snapshots = snapshots or []
        self.ingest_calls: list[tuple[str, int | None]] = []

    async def ingest(self, identity_sub, limit=None):
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
        self.transport_unavailable_providers: set[str] = set()
        self._rate_limits: list[tuple[str, EnrichmentRateLimitError]] = []
        self._keep_limiting_after_disable = False
        self._auth_reject_on_title: str | None = None
        self._auth_error: EnrichmentAuthError | None = None
        self._keep_rejecting_after_disable = False
        self.disabled_providers: list[str] = []

    def rate_limit_on(
        self, title: str, error: EnrichmentRateLimitError, *, keep_limiting_after_disable: bool = False
    ) -> None:
        """Simulate a rate limit on ``title``. By default, stops raising once ``disable_provider`` is
        called for the error's provider -- mirroring the real EnrichmentService, whose disabled client
        serves from cache instead of calling the provider again.
        """
        self._rate_limits.append((title, error))
        self._keep_limiting_after_disable = keep_limiting_after_disable

    def reject_with_auth_error(
        self, title: str, error: EnrichmentAuthError, *, keep_rejecting_after_disable: bool = False
    ) -> None:
        """Simulate a rejected key on ``title``. By default, stops rejecting once ``disable_provider`` is
        called for the error's provider -- mirroring the real EnrichmentService, whose disabled client can
        no longer raise the same error. ``keep_rejecting_after_disable=True`` simulates a hypothetical bug
        where the provider keeps rejecting anyway, to exercise the orchestrator's loop-guard branch.
        """
        self._auth_reject_on_title = title
        self._auth_error = error
        self._keep_rejecting_after_disable = keep_rejecting_after_disable

    def disable_provider(self, provider: str) -> None:
        self.disabled_providers.append(provider)

    async def enrich_game(self, title, *, title_id, is_ps5, genre_priorities, publisher_tier_rules, size_estimates):
        self.enrich_calls.append(title)
        for limited_title, error in self._rate_limits:
            if title == limited_title and (
                self._keep_limiting_after_disable or error.provider not in self.disabled_providers
            ):
                raise error
        if (
            title == self._auth_reject_on_title
            and self._auth_error is not None
            and (self._keep_rejecting_after_disable or self._auth_error.provider not in self.disabled_providers)
        ):
            raise self._auth_error
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
    def __init__(self, unmatched_override=None):
        self.upsert_calls = []
        self._unmatched_override = unmatched_override
        self.set_trophy_match_calls: list[tuple] = []
        self.refresh_trophy_progress_calls: list[tuple] = []
        self.last_title_id: str | None = None

    async def upsert_entry(
        self,
        identity_sub,
        game_id,
        *,
        native_ps5,
        ps4_eligible,
        owned_edition,
        winning_entitlement_id,
        product_id,
        title_id,
        is_active=True,
    ):
        self.upsert_calls.append((identity_sub, game_id, native_ps5, ps4_eligible, is_active))
        self.last_title_id = title_id

    async def get_unmatched_game_ids(self, identity_sub, game_ids):
        if self._unmatched_override is not None:
            return self._unmatched_override
        return list(game_ids)

    async def set_trophy_match(self, identity_sub, game_id, *, np_communication_id, method, percent_completed=None):
        self.set_trophy_match_calls.append((identity_sub, game_id, np_communication_id, method, percent_completed))

    async def refresh_trophy_progress(self, identity_sub, progress_by_np_id):
        self.refresh_trophy_progress_calls.append((identity_sub, progress_by_np_id))
        return len(progress_by_np_id)


class FakeTrophyClient:
    """Stands in for TrophyClient/CachedTrophyClient -- only the two methods match_trophies calls."""

    def __init__(self, *, exact_matches=None, fuzzy_titles=None):
        self._exact_matches = exact_matches or {}
        self._fuzzy_titles = fuzzy_titles or []
        self.trophy_titles_for_title_calls: list[list[str]] = []
        self.trophy_titles_call_count = 0

    async def trophy_titles_for_title(self, title_ids, online_id=None, account_id=None):
        self.trophy_titles_for_title_calls.append(title_ids)
        return self._exact_matches.get(title_ids[0], [])

    async def trophy_titles(self, online_id=None, account_id=None, limit=100):
        self.trophy_titles_call_count += 1
        return self._fuzzy_titles


def _snapshot(title="God of War", concept_id="c1", entitlement_id="e1", package_type="PS4GD", active=None):
    return EntitlementSnapshot(
        entitlement_id=entitlement_id,
        concept_id=concept_id,
        product_id="p1",
        title_id="t1",
        game_meta_name=title,
        concept_meta_name=None,
        title_meta_name=title,
        package_type=package_type,
        active=active,
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

    assert ingestion_service.ingest_calls == [("sub-1", None)]


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
    assert library_repository.upsert_calls == [
        ("sub-1", "game-1", games[0].native_ps5, games[0].ps4_eligible, games[0].active)
    ]

    assert games[0].winning_title_id == "t1"
    assert library_repository.last_title_id == "t1"


async def test_persist_and_link_carries_inactive_state_onto_the_library_entry():

    catalog_repository = FakeCatalogRepository()
    library_repository = FakeLibraryRepository()
    orchestrator = LibraryBuildOrchestrator(
        ingestion_service=FakeIngestionService(snapshots=[_snapshot(active=False)]),
        enrichment_service=FakeEnrichmentService(),
        enrichment_repository=FakeEnrichmentRepository(),
        catalog_repository=catalog_repository,
        library_repository=library_repository,
    )
    games = await orchestrator.canonicalize_current_entitlements("sub-1")

    await orchestrator.persist_and_link("sub-1", games)

    assert library_repository.upsert_calls[0][4] is False


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


async def test_enrich_delta_finishes_every_game_after_a_rate_limit_and_reports_the_ones_it_redoes():
    enrichment_service = FakeEnrichmentService()
    enrichment_service.rate_limit_on("Game B", EnrichmentRateLimitError("rawg", 120.0))
    orchestrator = _orchestrator(enrichment_service=enrichment_service)
    games = [_fake_canonical("Game A"), _fake_canonical("Game B"), _fake_canonical("Game C")]

    result = await orchestrator.enrich_delta(
        games, ["game-1", "game-2", "game-3"], publisher_tier_rules=[], size_estimates=[]
    )

    assert result.enriched_count == 3
    assert result.rate_limited_provider == "rawg"
    assert result.retry_after_seconds == 120.0
    assert enrichment_service.disabled_providers == ["rawg"]

    assert result.remaining_game_ids == ["game-2", "game-3"]
    assert enrichment_service.enrich_calls == ["Game A", "Game B", "Game B", "Game C"]


async def test_enrich_delta_still_enriches_every_game_when_the_very_first_one_is_rate_limited():
    enrichment_service = FakeEnrichmentService()
    enrichment_service.rate_limit_on("Game A", EnrichmentRateLimitError("opencritic", 3600.0))
    orchestrator = _orchestrator(enrichment_service=enrichment_service)
    games = [_fake_canonical("Game A"), _fake_canonical("Game B"), _fake_canonical("Game C")]

    result = await orchestrator.enrich_delta(
        games, ["game-1", "game-2", "game-3"], publisher_tier_rules=[], size_estimates=[]
    )

    assert result.enriched_count == 3
    assert result.remaining_game_ids == ["game-1", "game-2", "game-3"]


async def test_enrich_delta_reports_the_longest_backoff_when_both_providers_are_rate_limited():
    enrichment_service = FakeEnrichmentService()
    enrichment_service.rate_limit_on("Game A", EnrichmentRateLimitError("rawg", 120.0))
    enrichment_service.rate_limit_on("Game C", EnrichmentRateLimitError("opencritic", 3600.0))
    orchestrator = _orchestrator(enrichment_service=enrichment_service)
    games = [_fake_canonical("Game A"), _fake_canonical("Game B"), _fake_canonical("Game C")]

    result = await orchestrator.enrich_delta(
        games, ["game-1", "game-2", "game-3"], publisher_tier_rules=[], size_estimates=[]
    )

    assert result.enriched_count == 3
    assert result.rate_limited_provider == "opencritic"
    assert result.retry_after_seconds == 3600.0
    assert sorted(enrichment_service.disabled_providers) == ["opencritic", "rawg"]

    assert result.remaining_game_ids == ["game-1", "game-2", "game-3"]


async def test_enrich_delta_stops_when_a_rate_limited_provider_keeps_limiting_after_being_disabled():
    enrichment_service = FakeEnrichmentService()
    enrichment_service.rate_limit_on(
        "Game B", EnrichmentRateLimitError("rawg", 120.0), keep_limiting_after_disable=True
    )
    orchestrator = _orchestrator(enrichment_service=enrichment_service)
    games = [_fake_canonical("Game A"), _fake_canonical("Game B"), _fake_canonical("Game C")]

    result = await orchestrator.enrich_delta(
        games, ["game-1", "game-2", "game-3"], publisher_tier_rules=[], size_estimates=[]
    )

    assert result.enriched_count == 1
    assert result.rate_limited_provider == "rawg"
    assert result.remaining_game_ids == ["game-2", "game-3"]


async def test_enrich_delta_no_rate_limit_leaves_new_fields_at_their_defaults():
    enrichment_service = FakeEnrichmentService()
    orchestrator = _orchestrator(enrichment_service=enrichment_service)
    games = [_fake_canonical("Game A")]

    result = await orchestrator.enrich_delta(games, ["game-1"], publisher_tier_rules=[], size_estimates=[])

    assert result.rate_limited_provider is None
    assert result.retry_after_seconds is None
    assert result.remaining_game_ids == []
    assert result.rejected_providers == []


async def test_enrich_delta_degrades_instead_of_failing_on_a_rejected_key():
    enrichment_service = FakeEnrichmentService()
    enrichment_service.reject_with_auth_error("Game B", EnrichmentAuthError("rawg", "key rejected"))
    orchestrator = _orchestrator(enrichment_service=enrichment_service)
    games = [_fake_canonical("Game A"), _fake_canonical("Game B"), _fake_canonical("Game C")]

    result = await orchestrator.enrich_delta(
        games, ["game-1", "game-2", "game-3"], publisher_tier_rules=[], size_estimates=[]
    )

    assert result.enriched_count == 3
    assert result.rejected_providers == ["rawg"]
    assert enrichment_service.disabled_providers == ["rawg"]

    assert enrichment_service.enrich_calls == ["Game A", "Game B", "Game B", "Game C"]


async def test_enrich_delta_stops_rather_than_looping_if_the_same_provider_keeps_rejecting():
    enrichment_service = FakeEnrichmentService()
    enrichment_service.reject_with_auth_error(
        "Game B", EnrichmentAuthError("rawg", "key rejected"), keep_rejecting_after_disable=True
    )
    orchestrator = _orchestrator(enrichment_service=enrichment_service)
    games = [_fake_canonical("Game A"), _fake_canonical("Game B"), _fake_canonical("Game C")]

    result = await orchestrator.enrich_delta(
        games, ["game-1", "game-2", "game-3"], publisher_tier_rules=[], size_estimates=[]
    )

    assert result.enriched_count == 1
    assert result.rejected_providers == ["rawg"]
    assert enrichment_service.disabled_providers == ["rawg"]
    assert enrichment_service.enrich_calls == ["Game A", "Game B", "Game B"]


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

    assert ingestion_service.ingest_calls == [("sub-1", None)]


async def test_build_threads_rate_limit_fields_through_from_enrich_delta():
    enrichment_service = FakeEnrichmentService()
    enrichment_service.rate_limit_on("God of War", EnrichmentRateLimitError("opencritic", 45.0))
    ingestion_service = FakeIngestionService(pull_id="pull-1", snapshots=[_snapshot()])
    orchestrator = _orchestrator(ingestion_service=ingestion_service, enrichment_service=enrichment_service)

    result = await orchestrator.build("sub-1", publisher_tier_rules=[], size_estimates=[])

    assert result.rate_limited_provider == "opencritic"
    assert result.retry_after_seconds == 45.0
    assert result.remaining_game_ids == ["game-1"]


async def test_build_threads_rejected_providers_through_from_enrich_delta():
    enrichment_service = FakeEnrichmentService()
    enrichment_service.reject_with_auth_error("God of War", EnrichmentAuthError("rawg", "key rejected"))
    ingestion_service = FakeIngestionService(pull_id="pull-1", snapshots=[_snapshot()])
    orchestrator = _orchestrator(ingestion_service=ingestion_service, enrichment_service=enrichment_service)

    result = await orchestrator.build("sub-1", publisher_tier_rules=[], size_estimates=[])

    assert result.rejected_providers == ["rawg"]
    assert result.rate_limited_provider is None

    assert result.games_enriched == 1


def _fake_canonical(title, winning_title_id=None):
    from curator.catalog.canonicalization_service import CanonicalGame

    return CanonicalGame(
        canonical_title=title,
        native_ps5=False,
        ps4_eligible=True,
        franchise="",
        product_id="p1",
        concept_ids=("c1",),
        winning_entitlement_id="e1",
        winning_title_id=winning_title_id,
    )


def _trophy_title(name, np_communication_id, progress=50):
    from curator.psn.models import TrophyCounts, TrophyTitle

    return TrophyTitle(
        name=name,
        np_communication_id=np_communication_id,
        platforms=("PS4",),
        progress=progress,
        earned=TrophyCounts(gold=1),
        defined=TrophyCounts(gold=2),
    )


async def test_match_trophies_skipped_when_trophy_client_is_none():
    library_repository = FakeLibraryRepository()
    orchestrator = _orchestrator(library_repository=library_repository)
    games = [_fake_canonical("Game A")]

    result = await orchestrator.match_trophies("sub-1", games, ["game-1"], trophy_client=None)

    assert result.exact_matched_count == 0
    assert result.fuzzy_matched_count == 0
    assert result.attempted_count == 0
    assert library_repository.set_trophy_match_calls == []


async def test_match_trophies_attempts_no_new_matches_when_nothing_unmatched():
    """Identity matching is skipped, but progress is still refreshed.

    A settled library has nothing left to *match* -- that is what trophy_match_attempted_at protects --
    but percentages move every time the user plays, so the run still fetches titles once to refresh them.
    Skipping that entirely would freeze an established library's completion at whatever each game read on
    the day it was first seen.
    """
    library_repository = FakeLibraryRepository(unmatched_override=[])
    trophy_client = FakeTrophyClient(fuzzy_titles=[_trophy_title("Game A", "NPWR00001_00", progress=55)])
    orchestrator = _orchestrator(library_repository=library_repository)
    games = [_fake_canonical("Game A")]

    result = await orchestrator.match_trophies("sub-1", games, ["game-1"], trophy_client=trophy_client)

    assert result.attempted_count == 0
    assert library_repository.set_trophy_match_calls == []
    assert trophy_client.trophy_titles_for_title_calls == []
    assert trophy_client.trophy_titles_call_count == 1
    assert library_repository.refresh_trophy_progress_calls == [("sub-1", {"NPWR00001_00": 55})]


async def test_match_trophies_exact_match_for_ps4_title_id():
    library_repository = FakeLibraryRepository()
    trophy_client = FakeTrophyClient(
        exact_matches={"CUSA00001_00": [_trophy_title("God of War", "NPWR00001_00", progress=75)]}
    )
    orchestrator = _orchestrator(library_repository=library_repository)
    games = [_fake_canonical("God of War", winning_title_id="CUSA00001_00")]

    result = await orchestrator.match_trophies("sub-1", games, ["game-1"], trophy_client=trophy_client)

    assert result.exact_matched_count == 1
    assert result.fuzzy_matched_count == 0
    assert result.attempted_count == 1
    assert library_repository.set_trophy_match_calls == [("sub-1", "game-1", "NPWR00001_00", "exact", 75)]

    assert trophy_client.trophy_titles_call_count == 1


async def test_match_trophies_ps5_title_id_skips_exact_lookup_entirely():
    library_repository = FakeLibraryRepository()
    trophy_client = FakeTrophyClient(fuzzy_titles=[_trophy_title("Returnal", "NPWR00002_00", progress=42)])
    orchestrator = _orchestrator(library_repository=library_repository)
    games = [_fake_canonical("Returnal", winning_title_id="PPSA00001_00")]

    result = await orchestrator.match_trophies("sub-1", games, ["game-1"], trophy_client=trophy_client)

    assert trophy_client.trophy_titles_for_title_calls == []
    assert result.fuzzy_matched_count == 1
    assert library_repository.set_trophy_match_calls == [("sub-1", "game-1", "NPWR00002_00", "fuzzy", 42)]


async def test_match_trophies_exact_lookup_miss_falls_back_to_fuzzy():
    library_repository = FakeLibraryRepository()
    trophy_client = FakeTrophyClient(
        exact_matches={"CUSA00001_00": []},  # PSN has nothing for this title id
        fuzzy_titles=[_trophy_title("God of War", "NPWR00001_00", progress=75)],
    )
    orchestrator = _orchestrator(library_repository=library_repository)
    games = [_fake_canonical("God of War", winning_title_id="CUSA00001_00")]

    result = await orchestrator.match_trophies("sub-1", games, ["game-1"], trophy_client=trophy_client)

    assert trophy_client.trophy_titles_for_title_calls == [["CUSA00001_00"]]
    assert result.exact_matched_count == 0
    assert result.fuzzy_matched_count == 1
    assert library_repository.set_trophy_match_calls == [("sub-1", "game-1", "NPWR00001_00", "fuzzy", 75)]


async def test_match_trophies_no_confident_match_still_stamps_the_attempt():
    library_repository = FakeLibraryRepository()
    trophy_client = FakeTrophyClient(fuzzy_titles=[_trophy_title("Completely Unrelated", "NPWR99999_00")])
    orchestrator = _orchestrator(library_repository=library_repository)
    games = [_fake_canonical("God of War", winning_title_id="PPSA00001_00")]

    result = await orchestrator.match_trophies("sub-1", games, ["game-1"], trophy_client=trophy_client)

    assert result.exact_matched_count == 0
    assert result.fuzzy_matched_count == 0
    assert result.attempted_count == 1

    assert library_repository.set_trophy_match_calls == [("sub-1", "game-1", None, None, None)]


async def test_match_trophies_only_processes_games_with_no_persisted_match_yet():
    library_repository = FakeLibraryRepository(unmatched_override=["game-2"])
    trophy_client = FakeTrophyClient(fuzzy_titles=[_trophy_title("Game B", "NPWR2", progress=10)])
    orchestrator = _orchestrator(library_repository=library_repository)
    games = [
        _fake_canonical("Game A", winning_title_id="PPSA00001_00"),
        _fake_canonical("Game B", winning_title_id="PPSA00002_00"),
    ]

    result = await orchestrator.match_trophies("sub-1", games, ["game-1", "game-2"], trophy_client=trophy_client)

    assert result.attempted_count == 1
    assert [call[1] for call in library_repository.set_trophy_match_calls] == ["game-2"]
