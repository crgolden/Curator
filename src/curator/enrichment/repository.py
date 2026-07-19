"""Repository for the enrichment aggregate: game_enrichment, rawg_cache, opencritic_cache,
psn_catalog_cache, data_quality_flags.

Same shape as :class:`curator.persistence.repository.Repository`: backed by a shared
:class:`~psycopg_pool.AsyncConnectionPool`, raw parameterized SQL, frozen dataclass results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from psycopg_pool import AsyncConnectionPool

from curator.enrichment.opencritic_matcher import OpenCriticGame
from curator.enrichment.publisher_tier import PublisherTierRule
from curator.enrichment.rawg_matcher import normalize as normalize_rawg_title

if TYPE_CHECKING:
    from curator.enrichment.enrichment_service import EnrichmentResult


@dataclass(frozen=True, slots=True)
class RawgCacheEntry:
    """One row from ``rawg_cache``. ``raw is None`` means a confirmed no-match, distinct from "no row"."""

    normalized_title: str
    rawg_game_id: int | None
    raw: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class PsnCatalogCacheEntry:
    """One row from ``psn_catalog_cache``."""

    product_id: str
    concept_id: str | None
    genres: tuple[str, ...]
    star_rating: float | None
    publisher: str | None
    release_date: str | None
    cover_image_url: str | None


class EnrichmentRepository:
    """DAO over the enrichment aggregate's tables.

    :param pool: The shared connection pool.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def list_publisher_tier_rules(self) -> list[PublisherTierRule]:
        """Return every publisher-tier classification rule."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT tier_id, pattern, tier, match_kind FROM publisher_tiers")
            rows = await cur.fetchall()
        return [PublisherTierRule(tier_id=str(row[0]), pattern=row[1], tier=row[2], match_kind=row[3]) for row in rows]

    async def get_unenriched_game_ids(self, game_ids: list[str]) -> list[str]:
        """Return the subset of ``game_ids`` that have no ``game_enrichment`` row yet.

        The delta-enrichment check :class:`~curator.library.library_build_orchestrator.LibraryBuildOrchestrator`
        uses so a library rebuild only spends RAWG/OpenCritic/PSN-catalog quota on genuinely new games.

        :param game_ids: Candidate ``games.game_id`` values (e.g. a user's just-canonicalized library).
        :returns: The subset with no ``game_enrichment`` row.
        """
        if not game_ids:
            return []
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT game_id FROM unnest(%s::uuid[]) AS candidate(game_id)
                WHERE NOT EXISTS (
                    SELECT 1 FROM game_enrichment WHERE game_enrichment.game_id = candidate.game_id
                )
                """,
                (game_ids,),
            )
            rows = await cur.fetchall()
        return [str(row[0]) for row in rows]

    async def get_active_genres(self) -> list[tuple[str, str, int]]:
        """Return every active genre as ``(genre_id, name, priority)``.

        Callers derive both the ``name.lower() -> priority`` mapping
        :func:`~curator.scoring.genre_service.pick_genre_subgenre` needs and the ``name.lower() ->
        genre_id`` mapping needed to resolve a picked genre name back to its foreign key from this one read.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT genre_id, name, priority FROM genres WHERE active = true")
            rows = await cur.fetchall()
        return [(str(row[0]), row[1], row[2]) for row in rows]

    async def get_rawg_cache(self, title: str) -> RawgCacheEntry | None:
        """Return the cached RAWG lookup for a title, or ``None`` if never looked up.

        :param title: The canonical title (normalized internally before lookup).
        """
        normalized_title = normalize_rawg_title(title)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT normalized_title, rawg_game_id, raw FROM rawg_cache WHERE normalized_title = %s",
                (normalized_title,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return RawgCacheEntry(normalized_title=row[0], rawg_game_id=row[1], raw=row[2])

    async def save_rawg_cache(self, title: str, *, rawg_game_id: int | None, raw: dict[str, Any] | None) -> None:
        """Persist a RAWG lookup result (or confirmed no-match, when ``raw`` is ``None``).

        :param title: The canonical title (normalized internally before storing).
        :param rawg_game_id: The matched RAWG game id, or ``None`` on no match.
        :param raw: The full RAWG detail response, or ``None`` on no match.
        """
        normalized_title = normalize_rawg_title(title)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO rawg_cache (normalized_title, rawg_game_id, raw)
                VALUES (%s, %s, %s)
                ON CONFLICT (normalized_title) DO UPDATE SET
                    rawg_game_id = EXCLUDED.rawg_game_id,
                    raw = EXCLUDED.raw,
                    fetched_at = now()
                """,
                (normalized_title, rawg_game_id, json.dumps(raw) if raw is not None else None),
            )

    async def get_all_opencritic_games(self) -> list[OpenCriticGame]:
        """Return every cached OpenCritic game, for building the matcher's name index once per batch."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT oc_game_id, name, top_critic_score, tier, percent_recommended FROM opencritic_cache"
            )
            rows = await cur.fetchall()
        return [
            OpenCriticGame(
                oc_game_id=row[0], name=row[1], top_critic_score=row[2], tier=row[3] or "", percent_recommended=row[4]
            )
            for row in rows
        ]

    async def save_opencritic_games(self, games: list[OpenCriticGame]) -> None:
        """Upsert a batch of OpenCritic games (e.g. from one pagination run)."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            for game in games:
                await cur.execute(
                    """
                    INSERT INTO opencritic_cache (oc_game_id, name, top_critic_score, tier, percent_recommended)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (oc_game_id) DO UPDATE SET
                        name = EXCLUDED.name,
                        top_critic_score = EXCLUDED.top_critic_score,
                        tier = EXCLUDED.tier,
                        percent_recommended = EXCLUDED.percent_recommended,
                        fetched_at = now()
                    """,
                    (game.oc_game_id, game.name, game.top_critic_score, game.tier, game.percent_recommended),
                )

    async def get_opencritic_cursor(self, platform: str) -> int:
        """Return where OpenCritic catalog pagination for ``platform`` should resume from.

        Shared across every caller (the admin's catalog-wide re-scrape and every user's BYOK top-up) --
        see ``db/migrations/0004_user_enrichment_keys.sql`` for why this needs to be resumable and
        cooperative rather than per-caller.

        :param platform: The RapidAPI platform slug (``"ps4"`` or ``"ps5"``).
        :returns: ``0`` if no row exists yet (pagination never started for this platform).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT next_skip FROM opencritic_pagination_cursor WHERE platform = %s", (platform,))
            row = await cur.fetchone()
        return row[0] if row is not None else 0

    async def set_opencritic_cursor(self, platform: str, next_skip: int) -> None:
        """Persist where the next OpenCritic pagination call for ``platform`` should resume from.

        :param platform: The RapidAPI platform slug.
        :param next_skip: The new resume offset (``0`` when a pagination pass reached the end of the
            catalog, per :class:`curator.enrichment.opencritic_client.PaginationResult.exhausted`).
        """
        sql = (
            "INSERT INTO opencritic_pagination_cursor (platform, next_skip) VALUES (%s, %s) "
            "ON CONFLICT (platform) DO UPDATE SET next_skip = EXCLUDED.next_skip, updated_at = now()"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (platform, next_skip))

    async def get_psn_catalog_cache(self, product_id: str) -> PsnCatalogCacheEntry | None:
        """Return the cached official-PSN-catalog lookup for a product id, or ``None`` if never looked up."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT product_id, concept_id, genres, star_rating, publisher, release_date, cover_image_url
                FROM psn_catalog_cache WHERE product_id = %s
                """,
                (product_id,),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return PsnCatalogCacheEntry(
            product_id=row[0],
            concept_id=row[1],
            genres=tuple(row[2] or ()),
            star_rating=row[3],
            publisher=row[4],
            release_date=row[5],
            cover_image_url=row[6],
        )

    async def save_psn_catalog_cache(self, entry: PsnCatalogCacheEntry) -> None:
        """Persist an official-PSN-catalog lookup result."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO psn_catalog_cache (product_id, concept_id, genres, star_rating, publisher,
                                                release_date, cover_image_url)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (product_id) DO UPDATE SET
                    concept_id = EXCLUDED.concept_id,
                    genres = EXCLUDED.genres,
                    star_rating = EXCLUDED.star_rating,
                    publisher = EXCLUDED.publisher,
                    release_date = EXCLUDED.release_date,
                    cover_image_url = EXCLUDED.cover_image_url,
                    fetched_at = now()
                """,
                (
                    entry.product_id,
                    entry.concept_id,
                    list(entry.genres),
                    entry.star_rating,
                    entry.publisher,
                    entry.release_date,
                    entry.cover_image_url,
                ),
            )

    async def save_game_enrichment(
        self, game_id: str, genre_id: str | None, subgenre_id: str | None, result: EnrichmentResult
    ) -> None:
        """Upsert a game's resolved enrichment signals.

        :param game_id: The ``games.game_id`` this enrichment belongs to.
        :param genre_id: The resolved ``genres.genre_id`` for ``result.genre`` (already looked up by the
            caller -- this repository doesn't resolve genre names to ids itself).
        :param subgenre_id: The resolved ``genres.genre_id`` for ``result.subgenre``.
        :param result: The resolved enrichment signals.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO game_enrichment (
                    game_id, genre_id, subgenre_id, release_year, developer, publisher, esrb, multiplayer,
                    critical_score, oc_score, oc_tier, oc_percent_recommended, score_source, aaa_tier,
                    rawg_enriched, opencritic_enriched
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (game_id) DO UPDATE SET
                    genre_id = EXCLUDED.genre_id,
                    subgenre_id = EXCLUDED.subgenre_id,
                    release_year = EXCLUDED.release_year,
                    developer = EXCLUDED.developer,
                    publisher = EXCLUDED.publisher,
                    esrb = EXCLUDED.esrb,
                    multiplayer = EXCLUDED.multiplayer,
                    critical_score = EXCLUDED.critical_score,
                    oc_score = EXCLUDED.oc_score,
                    oc_tier = EXCLUDED.oc_tier,
                    oc_percent_recommended = EXCLUDED.oc_percent_recommended,
                    score_source = EXCLUDED.score_source,
                    aaa_tier = EXCLUDED.aaa_tier,
                    rawg_enriched = EXCLUDED.rawg_enriched,
                    opencritic_enriched = EXCLUDED.opencritic_enriched,
                    enriched_at = now()
                """,
                (
                    game_id,
                    genre_id,
                    subgenre_id,
                    result.release_year,
                    result.developer,
                    result.publisher,
                    result.esrb,
                    result.multiplayer,
                    result.critical_score,
                    result.oc_score,
                    result.oc_tier,
                    result.oc_percent_recommended,
                    result.score_source,
                    result.aaa_tier,
                    result.rawg_enriched,
                    result.opencritic_enriched,
                ),
            )

    async def flag_data_quality(self, flag_type: str, details: dict[str, Any]) -> None:
        """Record a detected data-quality issue for human review.

        :param flag_type: ``"same_title_different_product_id"``, ``"same_product_id_different_title"``,
            or ``"metadata_drift"``.
        :param details: Free-form detail payload (e.g. the colliding titles/ids).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO data_quality_flags (flag_type, details) VALUES (%s, %s)",
                (flag_type, json.dumps(details)),
            )
