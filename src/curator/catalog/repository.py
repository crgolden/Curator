"""Repository for the catalog aggregate: shared games/game_concepts/game_name_overrides, the per-user
ingestion layer (entitlement_pulls/entitlement_snapshots), and the canonicalization-rule tables
(exclusion_rules/franchise_rules/edition_ranks).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from psycopg_pool import AsyncConnectionPool

from curator.catalog.cover_art import SQUARE_COVER_ART_SQL
from curator.psn.store_client import StoreProduct
from curator.scoring.size_estimation_service import SizeEstimate


@dataclass(frozen=True, slots=True)
class GameSummary:
    """One row of ``GET /catalog/games``'s browsing result."""

    game_id: str
    canonical_title: str
    franchise: str | None
    genre: str | None
    aaa_tier: str | None
    cover_image_url: str | None = None
    store_product_id: str | None = None
    critical_score: float | None = None
    oc_score: float | None = None
    psn_rating: float | None = None
    percent_completed: int | None = None


class CatalogRepository:
    """DAO over the catalog aggregate's tables.

    :param pool: The shared connection pool.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def list_games(
        self,
        *,
        search: str | None = None,
        franchise: str | None = None,
        genre: str | None = None,
        aaa_tier: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[GameSummary], int]:
        """Return a page of the shared game catalog plus the total matching count.

        :param search: Optional case-insensitive title substring filter.
        :param franchise: Restrict to this exact franchise, if given.
        :param genre: Restrict to this exact genre name, if given.
        :param aaa_tier: Restrict to this publisher tier, if given.
        :param limit: Maximum number of rows to return.
        :param offset: Number of matching rows to skip (for pagination).
        """
        conditions: list[str] = []
        params: list[Any] = []
        if search:
            conditions.append("g.canonical_title ILIKE %s")
            params.append(f"%{search}%")
        if franchise is not None:
            conditions.append("g.franchise = %s")
            params.append(franchise)
        if genre is not None:
            conditions.append("gen.name = %s")
            params.append(genre)
        if aaa_tier is not None:
            conditions.append("ge.aaa_tier = %s")
            params.append(aaa_tier)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        base_query = f"""
            FROM games g
            LEFT JOIN game_enrichment ge ON ge.game_id = g.game_id
            LEFT JOIN genres gen ON gen.genre_id = ge.genre_id
            {where_clause}
        """

        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(f"SELECT COUNT(*) {base_query}", tuple(params))
            count_row = await cur.fetchone()
            assert count_row is not None
            total = count_row[0]

            await cur.execute(
                f"""
                SELECT g.game_id, g.canonical_title, g.franchise, gen.name, ge.aaa_tier,
                       {SQUARE_COVER_ART_SQL} AS cover_image_url,
                       (
                           SELECT pcc.store_product_id FROM psn_catalog_cache pcc
                           WHERE pcc.game_id = g.game_id AND pcc.store_product_id IS NOT NULL LIMIT 1
                       ) AS store_product_id,
                       ge.critical_score, ge.oc_score, ge.psn_rating
                {base_query}
                ORDER BY g.canonical_title, g.game_id
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
            rows = await cur.fetchall()
        return [
            GameSummary(
                game_id=str(row[0]),
                canonical_title=row[1],
                franchise=row[2],
                genre=row[3],
                aaa_tier=row[4],
                cover_image_url=row[5],
                store_product_id=row[6],
                critical_score=row[7],
                oc_score=row[8],
                psn_rating=row[9],
            )
            for row in rows
        ], total

    async def get_game(self, game_id: str, identity_sub: str | None = None) -> GameSummary | None:
        """Return one catalogued game, or ``None`` if no such game exists.

        :param game_id: The game's id.
        :param identity_sub: When given, populates ``percent_completed`` with that user's own trophy
            progress for this game; ``None`` leaves it unset, which is what an anonymous visitor sees.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT g.game_id, g.canonical_title, g.franchise, gen.name, ge.aaa_tier,
                       {SQUARE_COVER_ART_SQL} AS cover_image_url,
                       (
                           SELECT pcc.store_product_id FROM psn_catalog_cache pcc
                           WHERE pcc.game_id = g.game_id AND pcc.store_product_id IS NOT NULL LIMIT 1
                       ) AS store_product_id,
                       ge.critical_score, ge.oc_score, ge.psn_rating,
                       (
                           SELECT le.trophy_percent_completed FROM library_entries le
                           WHERE le.game_id = g.game_id AND le.identity_sub = %s
                       ) AS percent_completed
                FROM games g
                LEFT JOIN game_enrichment ge ON ge.game_id = g.game_id
                LEFT JOIN genres gen ON gen.genre_id = ge.genre_id
                WHERE g.game_id = %s
                """,
                (identity_sub, game_id),
            )
            row = await cur.fetchone()

        if row is None:
            return None
        return GameSummary(
            game_id=str(row[0]),
            canonical_title=row[1],
            franchise=row[2],
            genre=row[3],
            aaa_tier=row[4],
            cover_image_url=row[5],
            store_product_id=row[6],
            critical_score=row[7],
            oc_score=row[8],
            psn_rating=row[9],
            percent_completed=row[10],
        )

    async def list_genres(self) -> list[str]:
        """Return every active genre that is assigned to at least one game, most-preferred first.

        :returns: Genre names ordered by ``genres.priority`` ascending.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT gen.name
                FROM genres gen
                JOIN game_enrichment ge ON ge.genre_id = gen.genre_id
                WHERE gen.active = true
                GROUP BY gen.name, gen.priority
                ORDER BY gen.priority
                """
            )
            rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def backfill_store_products(self, products: Sequence[StoreProduct]) -> tuple[int, int]:
        """Seed the shared catalog from a storefront page: create missing ``games``, cache cover art.

        :param products: Store products from one page; filter to full games before calling.
        :returns: ``(games_created, covers_cached)``.
        """
        games_created = 0
        covers_cached = 0
        async with self._pool.connection() as conn, conn.cursor() as cur:
            for product in products:
                normalized_title = product.name.strip().lower()
                if not normalized_title:
                    continue

                await cur.execute("SELECT game_id FROM games WHERE normalized_title = %s", (normalized_title,))
                row = await cur.fetchone()
                if row is None:
                    await cur.execute(
                        "INSERT INTO games (canonical_title, normalized_title) VALUES (%s, %s) RETURNING game_id",
                        (product.name, normalized_title),
                    )
                    row = await cur.fetchone()
                    assert row is not None
                    games_created += 1
                game_id = str(row[0])

                if product.np_title_id:
                    await cur.execute(
                        """
                        INSERT INTO psn_catalog_cache (title_id, game_id, store_product_id, cover_image_url, fetched_at)
                        VALUES (%s, %s, %s, %s, now())
                        ON CONFLICT (title_id) DO UPDATE SET
                            game_id = EXCLUDED.game_id,
                            store_product_id = EXCLUDED.store_product_id,
                            cover_image_url = COALESCE(EXCLUDED.cover_image_url, psn_catalog_cache.cover_image_url),
                            fetched_at = now()
                        """,
                        (product.np_title_id, game_id, product.product_id, product.cover_image_url),
                    )
                    if product.cover_image_url:
                        covers_cached += 1
        return games_created, covers_cached

    async def game_exists(self, game_id: str) -> bool:
        """Return whether ``game_id`` names a game in the shared canonical catalog."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM games WHERE game_id = %s", (game_id,))
            return await cur.fetchone() is not None

    async def get_size_estimates(self) -> list[SizeEstimate]:
        """Return every install-size estimate row (per-title overrides and generic tier/genre-class bands)."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT estimate_id, title_pattern, aaa_tier, genre_class, platform, size_gb FROM size_estimates"
            )
            rows = await cur.fetchall()
        return [
            SizeEstimate(
                estimate_id=str(row[0]),
                title_pattern=row[1],
                aaa_tier=row[2],
                genre_class=row[3],
                platform=row[4],
                size_gb=float(row[5]),
            )
            for row in rows
        ]
