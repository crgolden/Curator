"""Enrichment orchestration: RAWG + OpenCritic + official PSN catalog metadata, merged into one game's
resolved enrichment signals.

Each external signal is independently cache-checked (via :class:`~curator.enrichment.repository.EnrichmentRepository`)
before any API call is made, so a re-enrichment pass only spends RAWG/OpenCritic/PSN-catalog quota on
titles that haven't already been resolved (or confirmed to have no match -- ``raw is None`` cache rows are
a real, durable "looked, found nothing" result, not "never looked").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from curator.enrichment.genre_reconciliation_service import reconcile_genres
from curator.enrichment.opencritic_client import OpenCriticApiError, OpenCriticClient
from curator.enrichment.opencritic_matcher import OpenCriticGame, build_name_index
from curator.enrichment.opencritic_matcher import find_match as find_opencritic_match
from curator.enrichment.publisher_tier import PublisherTierRule, classify_tier
from curator.enrichment.rawg_client import RawgApiError, RawgClient
from curator.enrichment.rawg_matcher import find_best_match as find_rawg_match
from curator.enrichment.repository import EnrichmentRepository, PsnCatalogCacheEntry
from curator.psn.catalog_client import CatalogClient
from curator.scoring.size_estimation_service import SizeEstimate, estimate_install_size_gb

_MULTIPLAYER_KEYWORDS = ("multiplayer", "co-op", "online", "pvp", "cooperative")
_OPENCRITIC_TOPUP_PLATFORMS = ("ps4", "ps5")
_OPENCRITIC_TOPUP_MAX_PAGES = 5
_AUTH_FAILURE_STATUS_CODES = (401, 403)


class EnrichmentAuthError(Exception):
    """Raised when a configured provider key is rejected (401/403) -- distinct from a transient failure
    (429/5xx) or the provider simply not being configured at all (which is not an error).

    Aborting the run fast on this, rather than continuing to grind through every remaining game with a
    key that's already known to be bad, is deliberate -- see
    :meth:`EnrichmentService._resolve_rawg`/:meth:`EnrichmentService._resolve_opencritic_topup`.

    :param provider: ``"rawg"`` or ``"opencritic"``.
    """

    def __init__(self, provider: str, message: str) -> None:
        super().__init__(message)
        self.provider = provider


@dataclass(frozen=True, slots=True)
class EnrichmentResult:
    """One game's resolved enrichment signals, ready to persist to ``game_enrichment``."""

    genre: str
    subgenre: str
    release_year: int | None
    developer: str | None
    publisher: str | None
    esrb: str | None
    multiplayer: bool | None
    critical_score: float | None
    oc_score: float | None
    oc_tier: str | None
    oc_percent_recommended: float | None
    psn_rating: float | None
    score_source: str | None
    aaa_tier: str
    rawg_enriched: bool
    opencritic_enriched: bool


@dataclass(frozen=True, slots=True)
class PsnCatalogLookup:
    """The official-PSN-catalog signals resolved for one product id: its genres (used for genre
    reconciliation) and its star rating (persisted as ``game_enrichment.psn_rating``)."""

    genres: list[str]
    star_rating: float | None


def _score_source(critical_score: float | None, oc_score: float | None) -> str | None:
    if critical_score is not None and oc_score is not None:
        return "RAWG + OC"
    if oc_score is not None:
        return "OC Only"
    if critical_score is not None:
        return "RAWG Only"
    return None


class EnrichmentService:
    """Orchestrates every enrichment signal for one game at a time.

    :param rawg_client: The caller's RAWG API client, or ``None`` if they haven't configured a RAWG key --
        ``enrich_game`` then skips the RAWG signal entirely for every game rather than failing. Curator
        never provisions a shared/fallback RAWG key (see ``curator.app._library_refresh_handler``).
    :param opencritic_client: The caller's OpenCritic API client, or ``None`` if they haven't configured an
        OpenCritic key. Used two ways: :meth:`refresh_opencritic_cache` (admin-only catalog-wide re-scrape)
        and, when this instance is built for a user's own library refresh, a bounded once-per-run top-up in
        :meth:`_resolve_opencritic` on a cache miss (see that method).
    :param catalog_client: The PSN official-catalog client. PSN's catalog API needs an authenticated
        session scoped to one user, unlike RAWG/OpenCritic, so callers that only need
        :meth:`refresh_opencritic_cache` (no PSN signal involved) may omit it; :meth:`enrich_game` then
        skips the PSN-genre signal entirely rather than failing.
    :param repository: The enrichment repository (caches + ``game_enrichment`` writes).
    """

    def __init__(
        self,
        *,
        rawg_client: RawgClient | None,
        opencritic_client: OpenCriticClient | None,
        catalog_client: CatalogClient | None = None,
        repository: EnrichmentRepository,
    ) -> None:
        self._rawg_client = rawg_client
        self._opencritic_client = opencritic_client
        self._catalog_client = catalog_client
        self._repository = repository
        self._opencritic_topup_attempted = False
        self.opencritic_topup_incomplete = False

    async def refresh_opencritic_cache(self, platforms: tuple[str, ...] = ("ps4", "ps5")) -> int:
        """Paginate OpenCritic's PS4/PS5 catalog into ``opencritic_cache``, resuming from the shared
        cursor (see ``db/migrations/0004_user_enrichment_keys.sql``).

        Call this on a schedule (it's the "background worker, not a bursty backfill" workflow the
        migration plan's rate-limit section calls for), not per-request -- OpenCritic's RapidAPI BASIC
        plan caps at 200 requests/day total. Shares its progress cursor with per-user BYOK top-ups
        (:meth:`_resolve_opencritic`), so both cooperatively sweep the same catalog over time.

        :param platforms: The RapidAPI platform slugs to paginate.
        :returns: The total number of games fetched across all platforms.
        :raises RuntimeError: If no OpenCritic client is configured (this method requires one -- unlike
            :meth:`enrich_game`, it has no "skip silently" fallback since it's the admin's own explicit
            re-scrape action).
        """
        if self._opencritic_client is None:
            raise RuntimeError("refresh_opencritic_cache requires an OpenCritic client.")

        total = 0
        for platform in platforms:
            start_skip = await self._repository.get_opencritic_cursor(platform)
            result = await self._opencritic_client.fetch_platform_games(platform, start_skip=start_skip)
            await self._repository.save_opencritic_games(result.games)
            await self._repository.set_opencritic_cursor(platform, result.next_skip)
            total += len(result.games)
        return total

    async def enrich_game(
        self,
        title: str,
        *,
        product_id: str | None,
        is_ps5: bool,
        genre_priorities: dict[str, int],
        publisher_tier_rules: list[PublisherTierRule],
        size_estimates: list[SizeEstimate],
    ) -> tuple[EnrichmentResult, float | None]:
        """Resolve every enrichment signal for one game.

        :param title: The game's canonical title.
        :param product_id: The game's PSN product id, if known (enables the official-catalog lookup).
        :param is_ps5: Whether to estimate install size for the PS5 edition.
        :param genre_priorities: ``name.lower() -> priority``, from
            :meth:`~curator.enrichment.repository.EnrichmentRepository.get_active_genres`.
        :param publisher_tier_rules: Every publisher-tier classification rule.
        :param size_estimates: Every install-size estimate row.
        :returns: The resolved :class:`EnrichmentResult`, plus its estimated install size in GB (kept
            separate since it isn't a ``game_enrichment`` column -- callers write it wherever their own
            per-user/per-console install-size tracking lives).
        """
        rawg_detail = await self._resolve_rawg(title)
        psn_catalog = await self._resolve_psn_catalog(product_id)
        psn_genres = psn_catalog.genres

        rawg_genres = [genre["name"] for genre in (rawg_detail or {}).get("genres", [])]
        genre, subgenre = reconcile_genres(psn_genres, rawg_genres, genre_priorities)

        developers = [d["name"] for d in (rawg_detail or {}).get("developers", [])]
        publishers = [p["name"] for p in (rawg_detail or {}).get("publishers", [])]
        developer = developers[0] if developers else None
        publisher = publishers[0] if publishers else None

        aaa_tier = classify_tier(publisher or "", publisher_tier_rules) or classify_tier(
            developer or "", publisher_tier_rules
        )
        aaa_tier = aaa_tier or "Indie"

        tags = [tag["name"].lower() for tag in (rawg_detail or {}).get("tags", [])]
        multiplayer = any(keyword in tag for keyword in _MULTIPLAYER_KEYWORDS for tag in tags) if tags else None

        metacritic = (rawg_detail or {}).get("metacritic")
        critical_score = float(metacritic) if metacritic else None

        oc_game = await self._resolve_opencritic(title)
        oc_score = oc_game.top_critic_score if oc_game else None
        oc_tier = oc_game.tier if oc_game else None
        oc_percent = oc_game.percent_recommended if oc_game else None

        released = (rawg_detail or {}).get("released") or ""
        release_year = int(released[:4]) if released[:4].isdigit() else None

        esrb = ((rawg_detail or {}).get("esrb_rating") or {}).get("name") if rawg_detail else None

        result = EnrichmentResult(
            genre=genre,
            subgenre=subgenre,
            release_year=release_year,
            developer=developer,
            publisher=publisher,
            esrb=esrb,
            multiplayer=multiplayer,
            critical_score=critical_score,
            oc_score=oc_score,
            oc_tier=oc_tier,
            oc_percent_recommended=oc_percent,
            psn_rating=psn_catalog.star_rating,
            score_source=_score_source(critical_score, oc_score),
            aaa_tier=aaa_tier,
            rawg_enriched=rawg_detail is not None,
            opencritic_enriched=oc_game is not None,
        )
        estimated_size = estimate_install_size_gb(title, genre, is_ps5, aaa_tier, size_estimates)
        return result, estimated_size

    async def _resolve_rawg(self, title: str) -> dict[str, Any] | None:
        if self._rawg_client is None:
            return None

        cached = await self._repository.get_rawg_cache(title)
        if cached is not None:
            return cached.raw

        try:
            candidates = await self._rawg_client.search_games(title)
        except RawgApiError as exc:
            if exc.status_code in _AUTH_FAILURE_STATUS_CODES:
                raise EnrichmentAuthError("rawg", str(exc)) from None
            return None  # transient (429/5xx) -- skip this game's RAWG signal, don't cache a false negative

        match = find_rawg_match(title, candidates)
        if match is None:
            await self._repository.save_rawg_cache(title, rawg_game_id=None, raw=None)
            return None

        try:
            detail = await self._rawg_client.fetch_detail(match.rawg_game_id)
        except RawgApiError as exc:
            if exc.status_code in _AUTH_FAILURE_STATUS_CODES:
                raise EnrichmentAuthError("rawg", str(exc)) from None
            return None

        await self._repository.save_rawg_cache(title, rawg_game_id=match.rawg_game_id, raw=detail)
        return detail

    async def _resolve_opencritic(self, title: str) -> OpenCriticGame | None:
        """Match ``title`` against the shared ``opencritic_cache``, topping it up at most once per
        :class:`EnrichmentService` instance (i.e. once per library-refresh run) via the caller's own key
        on the first cache miss -- see the class docstring and
        ``db/migrations/0004_user_enrichment_keys.sql``.
        """
        match = await self._match_opencritic_cache(title)
        if match is not None:
            return match

        if self._opencritic_client is None or self._opencritic_topup_attempted:
            return match

        self._opencritic_topup_attempted = True
        await self._run_opencritic_topup()
        return await self._match_opencritic_cache(title)

    async def _match_opencritic_cache(self, title: str) -> OpenCriticGame | None:
        games = await self._repository.get_all_opencritic_games()
        index, nospace_index = build_name_index(games)
        return find_opencritic_match(title, index, nospace_index)

    async def _run_opencritic_topup(self) -> None:
        assert self._opencritic_client is not None
        for platform in _OPENCRITIC_TOPUP_PLATFORMS:
            start_skip = await self._repository.get_opencritic_cursor(platform)
            try:
                result = await self._opencritic_client.fetch_platform_games(
                    platform, start_skip=start_skip, max_pages=_OPENCRITIC_TOPUP_MAX_PAGES
                )
            except OpenCriticApiError as exc:
                if exc.status_code in _AUTH_FAILURE_STATUS_CODES:
                    raise EnrichmentAuthError("opencritic", str(exc)) from None
                self.opencritic_topup_incomplete = True  # transient -- stop the top-up, don't fail the run
                return

            await self._repository.save_opencritic_games(result.games)
            await self._repository.set_opencritic_cursor(platform, result.next_skip)
            if not result.exhausted:
                self.opencritic_topup_incomplete = True

    async def _resolve_psn_catalog(self, product_id: str | None) -> PsnCatalogLookup:
        """Resolve a product id's official-PSN-catalog genres and star rating, cache-first.

        :param product_id: The game's PSN product id, or ``None`` if unknown.
        """
        if product_id is None or self._catalog_client is None:
            return PsnCatalogLookup(genres=[], star_rating=None)
        cached = await self._repository.get_psn_catalog_cache(product_id)
        if cached is not None:
            return PsnCatalogLookup(genres=list(cached.genres), star_rating=cached.star_rating)

        concept = await self._catalog_client.title_concept(product_id)
        await self._repository.save_psn_catalog_cache(
            PsnCatalogCacheEntry(
                product_id=product_id,
                concept_id=concept.concept_id,
                genres=concept.genres,
                star_rating=concept.star_rating,
                publisher=concept.publisher,
                release_date=concept.release_date,
                cover_image_url=concept.cover_image_url,
            )
        )
        return PsnCatalogLookup(genres=list(concept.genres), star_rating=concept.star_rating)
