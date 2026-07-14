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
from curator.enrichment.opencritic_client import OpenCriticClient
from curator.enrichment.opencritic_matcher import OpenCriticGame, build_name_index
from curator.enrichment.opencritic_matcher import find_match as find_opencritic_match
from curator.enrichment.publisher_tier import PublisherTierRule, classify_tier
from curator.enrichment.rawg_client import RawgClient
from curator.enrichment.rawg_matcher import find_best_match as find_rawg_match
from curator.enrichment.repository import EnrichmentRepository, PsnCatalogCacheEntry
from curator.psn.catalog_client import CatalogClient
from curator.scoring.size_estimation_service import SizeEstimate, estimate_install_size_gb

_MULTIPLAYER_KEYWORDS = ("multiplayer", "co-op", "online", "pvp", "cooperative")


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
    score_source: str | None
    aaa_tier: str


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

    :param rawg_client: The RAWG API client.
    :param opencritic_client: The OpenCritic API client (only used by :meth:`refresh_opencritic_cache`).
    :param catalog_client: The PSN official-catalog client. PSN's catalog API needs an authenticated
        session scoped to one user, unlike RAWG/OpenCritic, so callers that only need
        :meth:`refresh_opencritic_cache` (no PSN signal involved) may omit it; :meth:`enrich_game` then
        skips the PSN-genre signal entirely rather than failing.
    :param repository: The enrichment repository (caches + ``game_enrichment`` writes).
    """

    def __init__(
        self,
        *,
        rawg_client: RawgClient,
        opencritic_client: OpenCriticClient,
        catalog_client: CatalogClient | None = None,
        repository: EnrichmentRepository,
    ) -> None:
        self._rawg_client = rawg_client
        self._opencritic_client = opencritic_client
        self._catalog_client = catalog_client
        self._repository = repository

    async def refresh_opencritic_cache(self, platforms: tuple[str, ...] = ("ps4", "ps5")) -> int:
        """Paginate OpenCritic's full PS4/PS5 catalog into ``opencritic_cache``.

        Call this on a schedule (it's the "background worker, not a bursty backfill" workflow the
        migration plan's rate-limit section calls for), not per-request -- OpenCritic's RapidAPI BASIC
        plan caps at 200 requests/day total.

        :param platforms: The RapidAPI platform slugs to paginate.
        :returns: The total number of games fetched across all platforms.
        """
        total = 0
        for platform in platforms:
            games = await self._opencritic_client.fetch_platform_games(platform)
            await self._repository.save_opencritic_games(games)
            total += len(games)
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
        psn_genres = await self._resolve_psn_genres(product_id)

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
            score_source=_score_source(critical_score, oc_score),
            aaa_tier=aaa_tier,
        )
        estimated_size = estimate_install_size_gb(title, genre, is_ps5, aaa_tier, size_estimates)
        return result, estimated_size

    async def _resolve_rawg(self, title: str) -> dict[str, Any] | None:
        cached = await self._repository.get_rawg_cache(title)
        if cached is not None:
            return cached.raw

        candidates = await self._rawg_client.search_games(title)
        match = find_rawg_match(title, candidates)
        if match is None:
            await self._repository.save_rawg_cache(title, rawg_game_id=None, raw=None)
            return None

        detail = await self._rawg_client.fetch_detail(match.rawg_game_id)
        await self._repository.save_rawg_cache(title, rawg_game_id=match.rawg_game_id, raw=detail)
        return detail

    async def _resolve_opencritic(self, title: str) -> OpenCriticGame | None:
        games = await self._repository.get_all_opencritic_games()
        index, nospace_index = build_name_index(games)
        return find_opencritic_match(title, index, nospace_index)

    async def _resolve_psn_genres(self, product_id: str | None) -> list[str]:
        if product_id is None or self._catalog_client is None:
            return []
        cached = await self._repository.get_psn_catalog_cache(product_id)
        if cached is not None:
            return list(cached.genres)

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
        return list(concept.genres)
