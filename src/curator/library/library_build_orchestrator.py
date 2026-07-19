"""Library build orchestrator: composes ingest -> canonicalize -> persist -> enrich (delta) for one user.

Each stage is independently callable -- a caller/route can run just one (e.g. re-run enrichment without
re-ingesting) -- this orchestrator is a convenience that chains all of them for the common "give me an
up-to-date library" case (``POST /library/refresh``, tasks #12/#13), not a monolith the stages are coupled
into. Composite/rank scoring is deliberately NOT a stage here -- ``curator.scoring.scoring_service`` is
applied live at collection-generation time (:mod:`curator.collections`), not persisted onto
``game_enrichment`` by this orchestrator.
"""

from __future__ import annotations

from dataclasses import dataclass

from curator.catalog.canonicalization_service import CanonicalGame, canonicalize
from curator.catalog.repository import CatalogRepository
from curator.enrichment.enrichment_service import EnrichmentService
from curator.enrichment.publisher_tier import PublisherTierRule
from curator.enrichment.repository import EnrichmentRepository
from curator.library.ingestion_service import IngestionService
from curator.library.repository import LibraryRepository
from curator.scoring.size_estimation_service import SizeEstimate


@dataclass(frozen=True, slots=True)
class EnrichDeltaResult:
    """Summary of one :meth:`LibraryBuildOrchestrator.enrich_delta` call."""

    enriched_count: int
    rawg_enriched_titles: list[str]
    opencritic_enriched_titles: list[str]


@dataclass(frozen=True, slots=True)
class LibraryBuildResult:
    """Summary of one build run, for the caller/route to report back.

    :param rawg_enriched_titles: Titles newly enriched by RAWG this run (see
        ``curator.jobs.repository.JobRunsRepository.mark_succeeded``'s ``result_summary``).
    :param opencritic_enriched_titles: Titles newly enriched by OpenCritic this run.
    :param opencritic_topup_incomplete: Whether the OpenCritic pagination top-up (see
        ``curator.enrichment.enrichment_service.EnrichmentService._resolve_opencritic``) stopped early
        rather than exhausting the catalog -- a future refresh (by this user or any other, since the
        cache/cursor are shared) picks up where it left off.
    """

    pull_id: str
    games_canonicalized: int
    games_enriched: int
    rawg_enriched_titles: list[str]
    opencritic_enriched_titles: list[str]
    opencritic_topup_incomplete: bool


class LibraryBuildOrchestrator:
    """Composes the per-user library-build pipeline.

    :param ingestion_service: Fetches and records the caller's raw entitlements.
    :param catalog_repository: Shared-catalog reads (canonicalization rules) and writes (``games``/``game_concepts``).
    :param enrichment_service: Resolves RAWG/OpenCritic/PSN-catalog signals for a game.
    :param enrichment_repository: Enrichment-aggregate reads (delta check, genre lookup) and writes
        (``game_enrichment``) -- the same repository ``enrichment_service`` uses internally, injected
        separately here so this orchestrator never reaches through another collaborator's private state.
    :param library_repository: Per-user ``library_entries`` writes.
    """

    def __init__(
        self,
        *,
        ingestion_service: IngestionService,
        catalog_repository: CatalogRepository,
        enrichment_service: EnrichmentService,
        enrichment_repository: EnrichmentRepository,
        library_repository: LibraryRepository,
    ) -> None:
        self._ingestion_service = ingestion_service
        self._catalog_repository = catalog_repository
        self._enrichment_service = enrichment_service
        self._enrichment_repository = enrichment_repository
        self._library_repository = library_repository

    async def ingest(self, identity_sub: str) -> str:
        """Stage 1: fetch and record the caller's current entitlements.

        :returns: The new pull's id.
        """
        pull_id, _ = await self._ingestion_service.ingest(identity_sub)
        return pull_id

    async def canonicalize_current_entitlements(self, identity_sub: str, *, limit: int = 500) -> list[CanonicalGame]:
        """Stage 2: canonicalize the caller's current entitlements (re-fetches; does not re-ingest).

        Loads every canonicalization rule fresh from the catalog repository, so a rule change (a new
        exclusion pattern, a corrected name override) takes effect on the very next build without any
        code deploy.
        """
        _, snapshots = await self._ingestion_service.ingest(identity_sub, limit=limit)
        exclusion_rules = await self._catalog_repository.list_exclusion_rules()
        franchise_rules = await self._catalog_repository.list_franchise_rules()
        edition_ranks = await self._catalog_repository.get_edition_ranks()
        name_overrides = await self._catalog_repository.get_name_overrides()
        globally_excluded = await self._catalog_repository.get_globally_excluded_concept_ids()
        return canonicalize(
            snapshots,
            exclusion_rules=exclusion_rules,
            franchise_rules=franchise_rules,
            edition_ranks=edition_ranks,
            name_overrides=name_overrides,
            globally_excluded_concept_ids=globally_excluded,
        )

    async def persist_and_link(self, identity_sub: str, canonical_games: list[CanonicalGame]) -> list[str]:
        """Stage 3: merge each canonical game into the shared catalog and record this user's ownership.

        :returns: The resolved ``game_id`` for every canonical game, in the same order.
        """
        game_ids: list[str] = []
        for game in canonical_games:
            game_id = await self._catalog_repository.upsert_game(game)
            await self._library_repository.upsert_entry(
                identity_sub,
                game_id,
                native_ps5=game.native_ps5,
                ps4_eligible=game.ps4_eligible,
                owned_edition=game.canonical_title,
                winning_entitlement_id=game.winning_entitlement_id,
                product_id=game.product_id,
            )
            game_ids.append(game_id)
        return game_ids

    async def enrich_delta(
        self,
        canonical_games: list[CanonicalGame],
        game_ids: list[str],
        *,
        publisher_tier_rules: list[PublisherTierRule],
        size_estimates: list[SizeEstimate],
    ) -> EnrichDeltaResult:
        """Stage 4: enrich only the games that don't already have a ``game_enrichment`` row.

        :param canonical_games: The same list :meth:`persist_and_link` was called with (same order as
            ``game_ids``) -- needed for each game's title/product id/platform.
        :param game_ids: The resolved game ids from :meth:`persist_and_link`.
        :param publisher_tier_rules: Every publisher-tier classification rule.
        :param size_estimates: Every install-size estimate row.
        :returns: The :class:`EnrichDeltaResult` summary.
        """
        unenriched = set(await self._enrichment_repository.get_unenriched_game_ids(game_ids))
        genre_rows = await self._enrichment_repository.get_active_genres()
        genre_priorities = {name.lower(): priority for _, name, priority in genre_rows}
        genre_ids_by_name = {name.lower(): genre_id for genre_id, name, _ in genre_rows}

        enriched_count = 0
        rawg_enriched_titles: list[str] = []
        opencritic_enriched_titles: list[str] = []
        for game, game_id in zip(canonical_games, game_ids, strict=True):
            if game_id not in unenriched:
                continue
            result, _size = await self._enrichment_service.enrich_game(
                game.canonical_title,
                product_id=game.product_id,
                is_ps5=game.native_ps5,
                genre_priorities=genre_priorities,
                publisher_tier_rules=publisher_tier_rules,
                size_estimates=size_estimates,
            )
            genre_id = genre_ids_by_name.get(result.genre.lower())
            subgenre_id = genre_ids_by_name.get(result.subgenre.lower())
            await self._enrichment_repository.save_game_enrichment(game_id, genre_id, subgenre_id, result)
            enriched_count += 1
            if result.rawg_enriched:
                rawg_enriched_titles.append(game.canonical_title)
            if result.opencritic_enriched:
                opencritic_enriched_titles.append(game.canonical_title)
        return EnrichDeltaResult(
            enriched_count=enriched_count,
            rawg_enriched_titles=rawg_enriched_titles,
            opencritic_enriched_titles=opencritic_enriched_titles,
        )

    async def build(
        self,
        identity_sub: str,
        *,
        publisher_tier_rules: list[PublisherTierRule],
        size_estimates: list[SizeEstimate],
        limit: int = 500,
    ) -> LibraryBuildResult:
        """Run every stage in sequence: ingest, canonicalize, persist, enrich the delta.

        :param identity_sub: The Curator user id (Identity's ``sub``) to build a library for.
        :param publisher_tier_rules: Every publisher-tier classification rule.
        :param size_estimates: Every install-size estimate row.
        :param limit: Maximum number of entitlements to fetch.
        :returns: The :class:`LibraryBuildResult` summary.
        """
        pull_id, snapshots = await self._ingestion_service.ingest(identity_sub, limit=limit)
        exclusion_rules = await self._catalog_repository.list_exclusion_rules()
        franchise_rules = await self._catalog_repository.list_franchise_rules()
        edition_ranks = await self._catalog_repository.get_edition_ranks()
        name_overrides = await self._catalog_repository.get_name_overrides()
        globally_excluded = await self._catalog_repository.get_globally_excluded_concept_ids()
        canonical_games = canonicalize(
            snapshots,
            exclusion_rules=exclusion_rules,
            franchise_rules=franchise_rules,
            edition_ranks=edition_ranks,
            name_overrides=name_overrides,
            globally_excluded_concept_ids=globally_excluded,
        )

        game_ids = await self.persist_and_link(identity_sub, canonical_games)
        enrich_result = await self.enrich_delta(
            canonical_games, game_ids, publisher_tier_rules=publisher_tier_rules, size_estimates=size_estimates
        )

        return LibraryBuildResult(
            pull_id=pull_id,
            games_canonicalized=len(canonical_games),
            games_enriched=enrich_result.enriched_count,
            rawg_enriched_titles=enrich_result.rawg_enriched_titles,
            opencritic_enriched_titles=enrich_result.opencritic_enriched_titles,
            opencritic_topup_incomplete=self._enrichment_service.opencritic_topup_incomplete,
        )
