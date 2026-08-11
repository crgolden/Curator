"""Library build orchestrator: composes ingest -> canonicalize -> persist -> enrich (delta) for one user.

Each stage is independently callable -- a caller/route can run just one (e.g. re-run enrichment without
re-ingesting) -- this orchestrator is a convenience that chains all of them for the common "give me an
up-to-date library" case (``POST /library/refresh``, tasks #12/#13), not a monolith the stages are coupled
into. Composite/rank scoring is deliberately NOT a stage here -- ``curator.scoring.scoring_service`` is
applied live at collection-generation time (:mod:`curator.collections`), not persisted onto
``game_enrichment`` by this orchestrator.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from curator.catalog.canonicalization_service import CanonicalGame, canonicalize
from curator.catalog.repository import CatalogRepository
from curator.enrichment.enrichment_service import EnrichmentAuthError, EnrichmentRateLimitError, EnrichmentService
from curator.enrichment.publisher_tier import PublisherTierRule
from curator.enrichment.repository import EnrichmentRepository
from curator.library.ingestion_service import IngestionService
from curator.library.repository import LibraryRepository
from curator.psn.trophy_completion import match_titles
from curator.scoring.size_estimation_service import SizeEstimate

if TYPE_CHECKING:
    from curator.psn.models import TrophyTitle
    from curator.psn.trophy_cache import CachedTrophyClient
    from curator.psn.trophy_client import TrophyClient

_PS4_TITLE_ID_PREFIX = "CUSA"

_ENRICH_PROGRESS_EVERY = 25

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class EnrichDeltaResult:
    """Summary of one :meth:`LibraryBuildOrchestrator.enrich_delta` call.

    :param rate_limited_provider: ``"rawg"``/``"opencritic"`` if a provider's rate limit stopped
        enrichment early this call, else ``None``.
    :param retry_after_seconds: When it should be safe to resume, if ``rate_limited_provider`` is set.
    :param remaining_game_ids: Every game id not yet attempted this call, including the one that hit the
        rate limit (so it's retried first on resume) -- empty unless ``rate_limited_provider`` is set.
    :param rejected_providers: ``"rawg"``/``"opencritic"`` for every provider whose key was rejected
        (401/403) during this call -- the run still reaches ``succeeded``; this is how the caller reports
        "finished, RAWG skipped" instead of failing the whole refresh. See :meth:`EnrichmentService
        .disable_provider`.
    :param unavailable_providers: ``"rawg"``/``"opencritic"`` for every provider that became unreachable
        (repeated connect/read failures, not a rejection) during this call.
    """

    enriched_count: int
    rawg_enriched_titles: list[str]
    opencritic_enriched_titles: list[str]
    rate_limited_provider: str | None = None
    retry_after_seconds: float | None = None
    remaining_game_ids: list[str] = field(default_factory=list)
    rejected_providers: list[str] = field(default_factory=list)
    unavailable_providers: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class TrophyMatchResult:
    """Summary of one :meth:`LibraryBuildOrchestrator.match_trophies` call.

    :param exact_matched_count: Games matched via the exact PS4 title-id lookup
        (``TrophyClient.trophy_titles_for_title``).
    :param fuzzy_matched_count: Games matched via fuzzy name similarity (``curator.psn.trophy_completion
        .match_titles``) -- everything the exact lookup didn't resolve, including every PS5 title.
    :param attempted_count: Every game a match was attempted for this call, matched or not -- what
        ``curator.psn.trophy_completion`` stops re-attempting on the next refresh (see
        ``0014_library_entries_trophy_match.sql``'s ``trophy_match_attempted_at``).
    :param progress_updated_count: Library entries whose stored completion percentage was refreshed
        (``0015_library_entries_trophy_progress.sql``). Covers the whole matched library, not just this
        run's new matches, so it is routinely larger than the counts above.
    """

    exact_matched_count: int
    fuzzy_matched_count: int
    attempted_count: int
    progress_updated_count: int = 0


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
    :param rate_limited_provider: ``"rawg"``/``"opencritic"`` if a provider's rate limit stopped
        enrichment early this run, else ``None`` -- see :class:`EnrichDeltaResult`.
    :param retry_after_seconds: When it should be safe to resume, if ``rate_limited_provider`` is set.
    :param remaining_game_ids: Every game id not yet enriched this run, if ``rate_limited_provider`` is
        set -- what the continuation job (``curator.app._library_refresh_continuation_handler``) resumes.
    :param trophy_match: Summary of stage 5 (:meth:`LibraryBuildOrchestrator.match_trophies`) -- all-zero
        if ``build()`` was called with no ``trophy_client`` (``harvest_trophies`` disabled).
    :param rejected_providers: See :class:`EnrichDeltaResult`.
    :param unavailable_providers: See :class:`EnrichDeltaResult`.
    """

    pull_id: str
    games_canonicalized: int
    games_enriched: int
    rawg_enriched_titles: list[str]
    opencritic_enriched_titles: list[str]
    opencritic_topup_incomplete: bool
    rate_limited_provider: str | None = None
    retry_after_seconds: float | None = None
    remaining_game_ids: list[str] = field(default_factory=list)
    rejected_providers: list[str] = field(default_factory=list)
    unavailable_providers: list[str] = field(default_factory=list)
    trophy_match: TrophyMatchResult = field(
        default_factory=lambda: TrophyMatchResult(exact_matched_count=0, fuzzy_matched_count=0, attempted_count=0)
    )


async def enrich_games(
    enrichment_service: EnrichmentService,
    enrichment_repository: EnrichmentRepository,
    games: list[tuple[str, str, str | None, str | None, bool]],
    *,
    publisher_tier_rules: list[PublisherTierRule],
    size_estimates: list[SizeEstimate],
) -> EnrichDeltaResult:
    """Enrich each ``(game_id, title, product_id, title_id, is_ps5)`` tuple in order, stopping early on a
    provider rate limit -- but degrading, not stopping, on a rejected provider key.

    A module-level function (not a :class:`LibraryBuildOrchestrator` method) so it's callable without
    building a full orchestrator -- shared by :meth:`LibraryBuildOrchestrator.enrich_delta` (freshly
    canonicalized games from a normal refresh) and ``curator.app._library_refresh_continuation_handler``
    (a DB-looked-up subset resumed after a rate limit, with no re-canonicalization/re-ingestion needed, and
    thus no need for the ingestion/catalog/library repositories an orchestrator would otherwise require).

    A rejected key (:class:`~curator.enrichment.enrichment_service.EnrichmentAuthError`) is fundamentally
    different from a rate limit: a rate-limited provider will work again later, so the whole call stops and
    reports what's left to resume. A rejected key will never work again until the user re-saves it, so
    aborting the run would also throw away every other provider's signal, and PSN ingestion/trophy-matching
    downstream of enrichment, for every remaining game -- an optional signal taking down mandatory work.
    Instead, :meth:`~curator.enrichment.enrichment_service.EnrichmentService.disable_provider` makes that
    one provider behave like "not configured" for the rest of this call, and the *same* game is retried
    once so it still gets a chance at its other signals, rather than being skipped along with everything
    after it.

    :param games: Every game to attempt, in the order they should be enriched -- the order that ends up in
        ``remaining_game_ids`` if a rate limit stops iteration early.
    :param publisher_tier_rules: Every publisher-tier classification rule.
    :param size_estimates: Every install-size estimate row.
    :returns: The :class:`EnrichDeltaResult` summary.
    """
    genre_rows = await enrichment_repository.get_active_genres()
    genre_priorities = {name.lower(): priority for _, name, priority in genre_rows}
    genre_ids_by_name = {name.lower(): genre_id for genre_id, name, _ in genre_rows}

    enriched_count = 0
    rawg_enriched_titles: list[str] = []
    opencritic_enriched_titles: list[str] = []
    rejected_providers: list[str] = []
    index = 0
    logger.info("Enrichment starting for %d game(s).", len(games))
    while index < len(games):
        game_id, title, _product_id, title_id, is_ps5 = games[index]
        try:
            result, _size = await enrichment_service.enrich_game(
                title,
                title_id=title_id,
                is_ps5=is_ps5,
                genre_priorities=genre_priorities,
                publisher_tier_rules=publisher_tier_rules,
                size_estimates=size_estimates,
            )
        except EnrichmentRateLimitError as exc:
            logger.info(
                "Enrichment paused by %s rate limit after %d of %d game(s); resuming in %.0fs.",
                exc.provider,
                index,
                len(games),
                exc.retry_after_seconds,
            )
            return EnrichDeltaResult(
                enriched_count=enriched_count,
                rawg_enriched_titles=rawg_enriched_titles,
                opencritic_enriched_titles=opencritic_enriched_titles,
                rate_limited_provider=exc.provider,
                retry_after_seconds=exc.retry_after_seconds,
                remaining_game_ids=[remaining_game_id for remaining_game_id, *_ in games[index:]],
                rejected_providers=rejected_providers,
                unavailable_providers=sorted(enrichment_service.transport_unavailable_providers),
            )
        except EnrichmentAuthError as exc:
            if exc.provider in rejected_providers:
                break
            rejected_providers.append(exc.provider)
            enrichment_service.disable_provider(exc.provider)
            continue  # retry this same game now that the provider is disabled, don't advance index

        genre_id = genre_ids_by_name.get(result.genre.lower())
        subgenre_id = genre_ids_by_name.get(result.subgenre.lower())
        await enrichment_repository.save_game_enrichment(game_id, genre_id, subgenre_id, result)
        enriched_count += 1
        if result.rawg_enriched:
            rawg_enriched_titles.append(title)
        if result.opencritic_enriched:
            opencritic_enriched_titles.append(title)
        index += 1
        if index % _ENRICH_PROGRESS_EVERY == 0:
            logger.info("Enrichment progress: %d of %d game(s).", index, len(games))

    return EnrichDeltaResult(
        enriched_count=enriched_count,
        rawg_enriched_titles=rawg_enriched_titles,
        opencritic_enriched_titles=opencritic_enriched_titles,
        rejected_providers=rejected_providers,
        unavailable_providers=sorted(enrichment_service.transport_unavailable_providers),
    )


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

    async def canonicalize_current_entitlements(
        self, identity_sub: str, *, limit: int | None = None
    ) -> list[CanonicalGame]:
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
                title_id=game.winning_title_id,
                is_active=game.active,
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
        games = [
            (game_id, game.canonical_title, game.product_id, game.winning_title_id, game.native_ps5)
            for game, game_id in zip(canonical_games, game_ids, strict=True)
            if game_id in unenriched
        ]
        return await enrich_games(
            self._enrichment_service,
            self._enrichment_repository,
            games,
            publisher_tier_rules=publisher_tier_rules,
            size_estimates=size_estimates,
        )

    async def match_trophies(
        self,
        identity_sub: str,
        canonical_games: list[CanonicalGame],
        game_ids: list[str],
        *,
        trophy_client: TrophyClient | CachedTrophyClient | None,
    ) -> TrophyMatchResult:
        """Stage 5: persist each not-yet-matched game's PSN trophy-title identity.

        Moves the expensive part of ``curator.psn.trophy_completion``'s job from every future read (a
        full fuzzy name-match over the whole library, on every ``GET /library``/collection request) to
        this one ingestion-time pass: match once, persist ``np_communication_id``, and let reads become a
        cheap lookup by that column instead.

        Only attempts games with no persisted match yet (``LibraryRepository.get_unmatched_game_ids``) --
        a game once matched (successfully or not; see ``0014_library_entries_trophy_match.sql``'s
        ``trophy_match_attempted_at``) is never re-matched by a later refresh.

        For a PS4 title (a store title id starting with ``CUSA``), tries the exact PSN lookup first
        (``TrophyClient.trophy_titles_for_title``) -- real PSN API tooling confirms this only resolves for
        PS4 ids, so PS5 titles (``PPSA...``) and any PS4 title the exact lookup didn't resolve fall back to
        fuzzy name matching against one shared ``trophy_titles()`` fetch for the whole batch.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :param canonical_games: The same list :meth:`persist_and_link` was called with (same order as
            ``game_ids``).
        :param game_ids: The resolved game ids from :meth:`persist_and_link`.
        :param trophy_client: The user's trophy client, or ``None`` to skip this stage entirely -- the
            caller's responsibility to pass ``None`` when the user hasn't opted into ``harvest_trophies``
            (mirrors how a route checks that preference before touching any other trophy endpoint).
        :returns: The :class:`TrophyMatchResult` summary.
        """
        if trophy_client is None:
            return TrophyMatchResult(exact_matched_count=0, fuzzy_matched_count=0, attempted_count=0)

        unmatched_ids = set(await self._library_repository.get_unmatched_game_ids(identity_sub, game_ids))

        candidates = [
            (game_id, game) for game, game_id in zip(canonical_games, game_ids, strict=True) if game_id in unmatched_ids
        ]

        exact_matched_count = 0
        still_unmatched: list[tuple[str, str]] = []  # (game_id, canonical_title)
        for game_id, game in candidates:
            title_id = game.winning_title_id
            if title_id and title_id.startswith(_PS4_TITLE_ID_PREFIX):
                matches = await trophy_client.trophy_titles_for_title([title_id])
                exact = next(
                    (title for title in matches if title.np_communication_id and title.progress is not None), None
                )
                if exact is not None:
                    assert exact.np_communication_id is not None  # filtered above
                    await self._library_repository.set_trophy_match(
                        identity_sub,
                        game_id,
                        np_communication_id=exact.np_communication_id,
                        method="exact",
                        percent_completed=exact.progress,
                    )
                    exact_matched_count += 1
                    continue
            still_unmatched.append((game_id, game.canonical_title))

        fuzzy_matched_count = 0
        titles: list[TrophyTitle] = []
        if still_unmatched:
            titles = await trophy_client.trophy_titles(limit=500)
            fuzzy_matches = match_titles(titles, still_unmatched)
            for game_id, _title in still_unmatched:
                matched_title = fuzzy_matches.get(game_id)
                if matched_title is not None and matched_title.np_communication_id is not None:
                    await self._library_repository.set_trophy_match(
                        identity_sub,
                        game_id,
                        np_communication_id=matched_title.np_communication_id,
                        method="fuzzy",
                        percent_completed=matched_title.progress,
                    )
                    fuzzy_matched_count += 1
                else:
                    await self._library_repository.set_trophy_match(
                        identity_sub, game_id, np_communication_id=None, method=None
                    )

        if not titles:
            titles = await trophy_client.trophy_titles(limit=500)
        progress_by_np_id = {
            title.np_communication_id: title.progress
            for title in titles
            if title.np_communication_id and title.progress is not None
        }
        progress_updated_count = await self._library_repository.refresh_trophy_progress(identity_sub, progress_by_np_id)

        return TrophyMatchResult(
            exact_matched_count=exact_matched_count,
            fuzzy_matched_count=fuzzy_matched_count,
            attempted_count=len(candidates),
            progress_updated_count=progress_updated_count,
        )

    async def build(
        self,
        identity_sub: str,
        *,
        publisher_tier_rules: list[PublisherTierRule],
        size_estimates: list[SizeEstimate],
        limit: int | None = None,
        trophy_client: TrophyClient | CachedTrophyClient | None = None,
    ) -> LibraryBuildResult:
        """Run every stage in sequence: ingest, canonicalize, persist, enrich the delta, match trophies.

        :param identity_sub: The Curator user id (Identity's ``sub``) to build a library for.
        :param publisher_tier_rules: Every publisher-tier classification rule.
        :param size_estimates: Every install-size estimate row.
        :param limit: Maximum number of entitlements to fetch, or ``None`` (the default) to fetch every
            entitlement PSN reports -- see :meth:`~curator.psn.library_client.LibraryClient.entitlements`.
        :param trophy_client: Passed straight through to :meth:`match_trophies` -- ``None`` (the default)
            skips that stage entirely, which the caller should do whenever the user hasn't opted into
            ``harvest_trophies``.
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
        trophy_match_result = await self.match_trophies(
            identity_sub, canonical_games, game_ids, trophy_client=trophy_client
        )

        return LibraryBuildResult(
            pull_id=pull_id,
            games_canonicalized=len(canonical_games),
            games_enriched=enrich_result.enriched_count,
            rawg_enriched_titles=enrich_result.rawg_enriched_titles,
            opencritic_enriched_titles=enrich_result.opencritic_enriched_titles,
            opencritic_topup_incomplete=self._enrichment_service.opencritic_topup_incomplete,
            rate_limited_provider=enrich_result.rate_limited_provider,
            retry_after_seconds=enrich_result.retry_after_seconds,
            remaining_game_ids=enrich_result.remaining_game_ids,
            trophy_match=trophy_match_result,
            rejected_providers=enrich_result.rejected_providers,
            unavailable_providers=enrich_result.unavailable_providers,
        )
