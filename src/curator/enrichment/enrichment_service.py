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

import httpx

from curator.enrichment.genre_reconciliation_service import reconcile_genres
from curator.enrichment.opencritic_client import OpenCriticApiError, OpenCriticClientProtocol, OpenCriticNetworkError
from curator.enrichment.opencritic_matcher import OpenCriticGame, build_name_index
from curator.enrichment.opencritic_matcher import find_match as find_opencritic_match
from curator.enrichment.publisher_tier import PublisherTierRule, classify_tier
from curator.enrichment.rawg_client import RawgApiError, RawgClientProtocol
from curator.enrichment.rawg_matcher import find_best_match as find_rawg_match
from curator.enrichment.repository import EnrichmentRepository, PsnCatalogCacheEntry
from curator.psn.catalog_client import CatalogClient
from curator.scoring.size_estimation_service import SizeEstimate, estimate_install_size_gb

_MULTIPLAYER_KEYWORDS = ("multiplayer", "co-op", "online", "pvp", "cooperative")
_OPENCRITIC_TOPUP_PLATFORMS = ("ps4", "ps5")
_OPENCRITIC_TOPUP_MAX_PAGES = 5
_OPENCRITIC_ADMIN_REFRESH_MAX_PAGES = 20
_OPENCRITIC_ROTATE_ON_STATUS_CODES = (401, 403, 429)
_AUTH_FAILURE_STATUS_CODES = (401, 403)
_RATE_LIMIT_STATUS_CODE = 429
_DEFAULT_RATE_LIMIT_RETRY_SECONDS = 3600.0
_MAX_RATE_LIMIT_RETRY_SECONDS = 86400.0
_TRANSPORT_FAILURE_LIMIT = 3


def next_rate_limit_backoff_seconds(previous_retry_after_seconds: float) -> float:
    """Double a previous rate-limit wait, capped at 24h.

    The same escalation :class:`EnrichmentService` applies internally (via
    :meth:`EnrichmentService._rate_limit_retry_after`) across ``enrich_game`` calls within one instance --
    exposed here so ``curator.app._library_refresh_continuation_handler`` can seed a *fresh* instance (one
    per continuation queue message) with where the backoff left off, rather than resetting to the 1h
    default on every resume.
    """
    return min(previous_retry_after_seconds * 2, _MAX_RATE_LIMIT_RETRY_SECONDS)


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


class EnrichmentRateLimitError(Exception):
    """Raised when a provider returns 429 -- distinct from :class:`EnrichmentAuthError` (a bad key, not a
    quota) and from any other transient failure (5xx, which is still swallowed per-game -- see
    :meth:`EnrichmentService._resolve_rawg`/:meth:`EnrichmentService._run_opencritic_topup`).

    Callers (``curator.library.library_build_orchestrator.LibraryBuildOrchestrator.enrich_delta``) stop
    enrichment immediately on this rather than continuing to burn through every remaining game against a
    provider that's already exhausted for the window -- each subsequent call would fail identically.

    :param provider: ``"rawg"`` or ``"opencritic"``.
    :param retry_after_seconds: When enrichment should be safe to resume -- from the provider's own
        ``Retry-After`` response header when present, otherwise a heuristic default (see
        :data:`_DEFAULT_RATE_LIMIT_RETRY_SECONDS`).
    """

    def __init__(self, provider: str, retry_after_seconds: float) -> None:
        super().__init__(f"{provider} rate limit hit; retry after {retry_after_seconds:.0f}s")
        self.provider = provider
        self.retry_after_seconds = retry_after_seconds


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
    aaa_tier: str | None
    rawg_enriched: bool
    opencritic_enriched: bool


@dataclass(frozen=True, slots=True)
class PsnCatalogLookup:
    """The official-PSN-catalog signals resolved for one product id.

    Every field here takes precedence over its RAWG equivalent in :meth:`EnrichmentService.enrich_game`.
    """

    genres: list[str]
    star_rating: float | None
    publisher: str | None = None
    release_date: str | None = None
    content_rating: str | None = None


def _release_year(released: str | None) -> int | None:
    """Read a four-digit year off a leading ISO-8601 date, or ``None`` if there isn't one.

    Accepts both shapes the two sources use: PSN's full timestamp (``2018-10-05T04:00:00Z``) and RAWG's
    bare date (``2018-10-05``).
    """
    prefix = (released or "")[:4]
    return int(prefix) if prefix.isdigit() else None


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
        OpenCritic key. Used for a user's own library refresh: a bounded once-per-run top-up in
        :meth:`_resolve_opencritic` on a cache miss (see that method). Never populated on the same instance
        as ``opencritic_admin_clients`` -- a per-user instance always has at most one BYOK key, never
        rotated.
    :param opencritic_admin_clients: Every admin OpenCritic client, one per configured key, in rotation
        order -- used only by :meth:`refresh_opencritic_cache` (the admin catalog-wide re-scrape). Empty for
        a per-user instance.
    :param catalog_client: The PSN official-catalog client. PSN's catalog API needs an authenticated
        session scoped to one user, unlike RAWG/OpenCritic, so callers that only need
        :meth:`refresh_opencritic_cache` (no PSN signal involved) may omit it; :meth:`enrich_game` then
        skips the PSN-genre signal entirely rather than failing.
    :param repository: The enrichment repository (caches + ``game_enrichment`` writes).
    :param rate_limit_backoff_seconds: Per-provider starting point for the doubling backoff (see
        :meth:`_rate_limit_retry_after`), keyed by ``"rawg"``/``"opencritic"``. A fresh
        :class:`EnrichmentService` is built per library-refresh-continuation queue message (one run's retry
        chain spans many messages, each with its own instance) -- passing the previous message's
        ``retry_after_seconds`` (already doubled -- see :func:`next_rate_limit_backoff_seconds`) here is
        what makes the backoff escalate across the whole chain instead of resetting to the 1h default on
        every resume. Defaults to empty (every provider starts from the default) for a normal, non-resumed
        refresh.
    """

    def __init__(
        self,
        *,
        rawg_client: RawgClientProtocol | None,
        opencritic_client: OpenCriticClientProtocol | None,
        catalog_client: CatalogClient | None = None,
        repository: EnrichmentRepository,
        rate_limit_backoff_seconds: dict[str, float] | None = None,
        opencritic_admin_clients: tuple[OpenCriticClientProtocol, ...] = (),
    ) -> None:
        self._rawg_client = rawg_client
        self._opencritic_client = opencritic_client
        self._opencritic_admin_clients = opencritic_admin_clients
        self._catalog_client = catalog_client
        self._repository = repository
        self._opencritic_topup_attempted = False
        self.opencritic_topup_incomplete = False
        self._next_rate_limit_backoff_seconds: dict[str, float] = dict(rate_limit_backoff_seconds or {})
        self._consecutive_transport_failures: dict[str, int] = {}
        self.transport_unavailable_providers: set[str] = set()

    @property
    def has_rawg_client(self) -> bool:
        """Whether a RAWG client is configured -- lets callers (e.g. ``curator.app._enrichment_run_handler``)
        report a consistent "not configured" status for this provider without reaching into private state."""
        return self._rawg_client is not None

    @property
    def has_opencritic_client(self) -> bool:
        """Whether an OpenCritic client is configured -- see :attr:`has_rawg_client`. Checks both the
        per-user ``opencritic_client`` and the admin ``opencritic_admin_clients`` tuple; only one is ever
        populated on a given instance."""
        return self._opencritic_client is not None or bool(self._opencritic_admin_clients)

    @property
    def has_catalog_client(self) -> bool:
        """Whether an official-PSN-catalog client is configured -- see :attr:`has_rawg_client`. Always
        ``False`` for the admin catalog-wide singleton (``curator.app``'s ``enrichment_service``), since
        that signal needs a per-user authenticated ``PsnSession``."""
        return self._catalog_client is not None

    def disable_provider(self, provider: str) -> None:
        """Stop using ``provider`` for the remainder of this instance's lifetime.

        Called by :func:`curator.library.library_build_orchestrator.enrich_games` when
        :class:`EnrichmentAuthError` is raised for ``provider`` -- the key that produced it is already
        known bad, so every subsequent call is made to behave exactly like "not configured" (skipped
        silently) instead of raising the same error again for every remaining game in the run.

        :param provider: ``"rawg"`` or ``"opencritic"``.
        """
        if provider == "rawg":
            self._rawg_client = None
        elif provider == "opencritic":
            self._opencritic_client = None

    def _note_transport_failure(self, provider: str) -> None:
        """Count a connect/read failure against ``provider``, disabling it at
        :data:`_TRANSPORT_FAILURE_LIMIT` consecutive failures.

        :param provider: ``"rawg"`` or ``"opencritic"``.
        """
        failures = self._consecutive_transport_failures.get(provider, 0) + 1
        self._consecutive_transport_failures[provider] = failures
        if failures >= _TRANSPORT_FAILURE_LIMIT:
            self.transport_unavailable_providers.add(provider)
            self.disable_provider(provider)

    def _note_transport_success(self, provider: str) -> None:
        """Clear ``provider``'s consecutive-transport-failure count after a request that got through."""
        self._consecutive_transport_failures.pop(provider, None)

    def _rate_limit_retry_after(self, provider: str, hinted_seconds: float | None) -> float:
        """Resolve how long to wait before retrying ``provider``, on a 429.

        Prefers the provider's own ``Retry-After`` hint when given (clamped to the same cap the fallback
        heuristic respects, so a bogus/huge header value can't schedule a continuation message past the
        Service Bus queue's message TTL and get silently dropped instead of delayed). Otherwise falls back
        to a doubling heuristic (1h, 2h, 4h, ... capped at 24h) per provider, per :class:`EnrichmentService`
        instance -- neither RAWG nor OpenCritic reliably documents a reset header for quota exhaustion, so
        repeated 429s within the same run back off rather than retrying at a fixed interval indefinitely.
        """
        if hinted_seconds is not None:
            return min(hinted_seconds, _MAX_RATE_LIMIT_RETRY_SECONDS)
        current = self._next_rate_limit_backoff_seconds.get(provider, _DEFAULT_RATE_LIMIT_RETRY_SECONDS)
        self._next_rate_limit_backoff_seconds[provider] = next_rate_limit_backoff_seconds(current)
        return current

    async def refresh_opencritic_cache(self, platforms: tuple[str, ...] = ("ps4", "ps5")) -> int:
        """Paginate OpenCritic's PS4/PS5 catalog into ``opencritic_cache``, resuming from the shared
        cursor (see ``db/migrations/0004_user_enrichment_keys.sql``), rotating across every configured
        admin key on a 401/403/429 (see :meth:`_refresh_opencritic_platform`).

        Call this on a schedule (it's the "background worker, not a bursty backfill" workflow the
        migration plan's rate-limit section calls for), not per-request -- OpenCritic's RapidAPI BASIC
        plan caps at 200 requests/day total per key, and each call is bounded to
        :data:`_OPENCRITIC_ADMIN_REFRESH_MAX_PAGES` pages so one run can't burn a whole key's daily quota
        trying to sweep the entire catalog at once. Shares its progress cursor with per-user BYOK top-ups
        (:meth:`_resolve_opencritic`), so both cooperatively sweep the same catalog over time.

        :param platforms: The RapidAPI platform slugs to paginate.
        :returns: The total number of games fetched across all platforms.
        :raises RuntimeError: If no admin OpenCritic client is configured (this method requires at least
            one -- unlike :meth:`enrich_game`, it has no "skip silently" fallback since it's the admin's
            own explicit re-scrape action).
        :raises EnrichmentAuthError: If every configured key was rejected (401/403).
        :raises EnrichmentRateLimitError: If every configured key was rate-limited (429).
        """
        if not self._opencritic_admin_clients:
            raise RuntimeError("refresh_opencritic_cache requires at least one admin OpenCritic client.")

        total = 0
        for platform in platforms:
            total += await self._refresh_opencritic_platform(platform)
        return total

    async def _refresh_opencritic_platform(self, platform: str) -> int:
        """Sweep one platform's OpenCritic catalog (bounded by :data:`_OPENCRITIC_ADMIN_REFRESH_MAX_PAGES`
        pages), rotating across :attr:`_opencritic_admin_clients` on a 401/403/429 from the current key.

        Re-reads the shared cursor before each key's attempt (rather than once up front), so a key that
        rotates in after a prior key's partial progress resumes past it instead of replaying the same
        pages -- the gap the single shared-closure retry in the old ``RotatingOpenCriticClient`` had, since
        it captured ``start_skip`` once before any key was tried.

        :returns: The number of games fetched for this platform -- ``0`` if every key failed with a
            non-rotating error (a 5xx/network blip, not key-specific), matching the prior single-client
            swallow-and-move-on behavior instead of failing the whole run over a transient blip.
        :raises EnrichmentAuthError: If every configured key was rejected (401/403).
        :raises EnrichmentRateLimitError: If every configured key was rate-limited (429).
        """
        last_exc: OpenCriticApiError | None = None
        for client in self._opencritic_admin_clients:
            start_skip = await self._repository.get_opencritic_cursor(platform)
            try:
                result = await client.fetch_platform_games(
                    platform, start_skip=start_skip, max_pages=_OPENCRITIC_ADMIN_REFRESH_MAX_PAGES
                )
            except (OpenCriticApiError, OpenCriticNetworkError) as exc:
                if exc.partial_games:
                    await self._repository.save_opencritic_games(exc.partial_games)
                if exc.partial_next_skip is not None:
                    await self._repository.set_opencritic_cursor(platform, exc.partial_next_skip)
                if isinstance(exc, OpenCriticApiError) and exc.status_code in _OPENCRITIC_ROTATE_ON_STATUS_CODES:
                    last_exc = exc
                    continue
                return 0
            await self._repository.save_opencritic_games(result.games)
            await self._repository.set_opencritic_cursor(platform, result.next_skip)
            return len(result.games)

        assert last_exc is not None  # every client was tried and rotated past (401/403/429), so this is set
        if last_exc.status_code in _AUTH_FAILURE_STATUS_CODES:
            raise EnrichmentAuthError("opencritic", str(last_exc)) from None
        retry_after = self._rate_limit_retry_after("opencritic", last_exc.retry_after_seconds)
        raise EnrichmentRateLimitError("opencritic", retry_after) from None

    async def enrich_game(
        self,
        title: str,
        *,
        title_id: str | None,
        is_ps5: bool,
        genre_priorities: dict[str, int],
        publisher_tier_rules: list[PublisherTierRule],
        size_estimates: list[SizeEstimate],
    ) -> tuple[EnrichmentResult, float | None]:
        """Resolve every enrichment signal for one game.

        :param title: The game's canonical title.
        :param title_id: The game's PSN store/content title id (npTitleId), if known -- enables the
            official-catalog lookup (:meth:`_resolve_psn_catalog`), which requires this identifier, not a
            PSN product id.
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
        psn_catalog = await self._resolve_psn_catalog(title_id)
        psn_genres = psn_catalog.genres

        rawg_genres = [genre["name"] for genre in (rawg_detail or {}).get("genres", [])]
        genre, subgenre = reconcile_genres(psn_genres, rawg_genres, genre_priorities)

        developers = [d["name"] for d in (rawg_detail or {}).get("developers", [])]
        publishers = [p["name"] for p in (rawg_detail or {}).get("publishers", [])]
        developer = developers[0] if developers else None
        publisher = psn_catalog.publisher or (publishers[0] if publishers else None)

        aaa_tier = classify_tier(publisher, publisher_tier_rules) or classify_tier(developer, publisher_tier_rules)

        tags = [tag["name"].lower() for tag in (rawg_detail or {}).get("tags", [])]
        multiplayer = any(keyword in tag for keyword in _MULTIPLAYER_KEYWORDS for tag in tags) if tags else None

        metacritic = (rawg_detail or {}).get("metacritic")
        critical_score = float(metacritic) if metacritic else None

        oc_game = await self._resolve_opencritic(title)
        oc_score = oc_game.top_critic_score if oc_game else None
        oc_tier = oc_game.tier if oc_game else None
        oc_percent = oc_game.percent_recommended if oc_game else None

        rawg_released = (rawg_detail or {}).get("released") or ""
        release_year = _release_year(psn_catalog.release_date) or _release_year(rawg_released)

        rawg_esrb = ((rawg_detail or {}).get("esrb_rating") or {}).get("name") if rawg_detail else None
        esrb = psn_catalog.content_rating or rawg_esrb

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
        estimated_size = estimate_install_size_gb(title, genre, is_ps5, aaa_tier or "Indie", size_estimates)
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
            if exc.status_code == _RATE_LIMIT_STATUS_CODE:
                retry_after = self._rate_limit_retry_after("rawg", exc.retry_after_seconds)
                raise EnrichmentRateLimitError("rawg", retry_after) from None
            self._note_transport_success("rawg")
            return None
        except httpx.HTTPError:
            self._note_transport_failure("rawg")
            return None

        self._note_transport_success("rawg")
        match = find_rawg_match(title, candidates)
        if match is None:
            await self._repository.save_rawg_cache(title, rawg_game_id=None, raw=None)
            return None

        try:
            detail = await self._rawg_client.fetch_detail(match.rawg_game_id)
        except RawgApiError as exc:
            if exc.status_code in _AUTH_FAILURE_STATUS_CODES:
                raise EnrichmentAuthError("rawg", str(exc)) from None
            if exc.status_code == _RATE_LIMIT_STATUS_CODE:
                retry_after = self._rate_limit_retry_after("rawg", exc.retry_after_seconds)
                raise EnrichmentRateLimitError("rawg", retry_after) from None
            self._note_transport_success("rawg")
            return None
        except httpx.HTTPError:
            self._note_transport_failure("rawg")
            return None

        self._note_transport_success("rawg")
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
                if exc.status_code == _RATE_LIMIT_STATUS_CODE:
                    retry_after = self._rate_limit_retry_after("opencritic", exc.retry_after_seconds)
                    raise EnrichmentRateLimitError("opencritic", retry_after) from None
                self.opencritic_topup_incomplete = True
                return
            except httpx.HTTPError:
                self.opencritic_topup_incomplete = True
                return

            await self._repository.save_opencritic_games(result.games)
            await self._repository.set_opencritic_cursor(platform, result.next_skip)
            if not result.exhausted:
                self.opencritic_topup_incomplete = True

    async def _resolve_psn_catalog(self, title_id: str | None) -> PsnCatalogLookup:
        """Resolve a title id's official-PSN-catalog signals, cache-first.

        :param title_id: The game's PSN store/content title id (npTitleId), or ``None`` if unknown. PSN's
            catalog "concepts" endpoint (:meth:`~curator.psn.catalog_client.CatalogClient.title_concept`)
            requires this identifier specifically -- a PSN product id is a different id PSN's own data
            model treats as distinct, and passing one here silently resolves nothing.
        """
        if title_id is None or self._catalog_client is None:
            return PsnCatalogLookup(genres=[], star_rating=None)
        cached = await self._repository.get_psn_catalog_cache(title_id)
        if cached is not None:
            return PsnCatalogLookup(
                genres=list(cached.genres),
                star_rating=cached.star_rating,
                publisher=cached.publisher,
                release_date=cached.release_date,
                content_rating=cached.content_rating,
            )

        try:
            concept = await self._catalog_client.title_concept(title_id)
        except httpx.HTTPError:
            return PsnCatalogLookup(genres=[], star_rating=None)
        await self._repository.save_psn_catalog_cache(
            PsnCatalogCacheEntry(
                title_id=title_id,
                concept_id=concept.concept_id,
                genres=concept.genres,
                star_rating=concept.star_rating,
                publisher=concept.publisher,
                release_date=concept.release_date,
                cover_image_url=concept.cover_image_url,
                content_rating=concept.content_rating,
                rating_authority=concept.rating_authority,
            )
        )
        return PsnCatalogLookup(
            genres=list(concept.genres),
            star_rating=concept.star_rating,
            publisher=concept.publisher,
            release_date=concept.release_date,
            content_rating=concept.content_rating,
        )
