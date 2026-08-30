"""Repository for the catalog aggregate: shared games/game_concepts/game_name_overrides, the per-user
ingestion layer (entitlement_pulls/entitlement_snapshots), and the canonicalization-rule tables
(exclusion_rules/franchise_rules/edition_ranks).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from psycopg.types.json import Jsonb
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


GAME_UPSERT_ADVISORY_LOCK_CLASS = 1
"""Must equal ``Functions``' own ``CuratorAdvisoryLocks.GameUpsert`` -- Postgres keeps the single-bigint
and two-int ``pg_advisory_xact_lock`` forms in separate lock spaces, so both the form and this classid have
to match for the two repos to contend for the same lock."""


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

    async def list_genre_vocabulary(self) -> list[str]:
        """Return the whole ``genres`` reference table, including names no game is assigned to.

        Distinct from :meth:`list_genres`, which answers "what can browsing be filtered by" and therefore
        joins ``game_enrichment``. Vocabulary drift is a question about the reference table itself, so an
        unassigned seeded genre must count as present rather than as missing.

        :returns: Every ``genres.name``, ordered by ``genres.priority`` ascending.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT name FROM genres ORDER BY priority")
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
                if product.name is None:
                    continue

                normalized_title = product.name.lower()
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
                        INSERT INTO psn_catalog_cache
                            (title_id, game_id, store_product_id, cover_image_url, raw, fetched_at)
                        VALUES (%s, %s, %s, %s, %s, now())
                        ON CONFLICT (title_id) DO UPDATE SET
                            game_id = EXCLUDED.game_id,
                            store_product_id = EXCLUDED.store_product_id,
                            cover_image_url = COALESCE(EXCLUDED.cover_image_url, psn_catalog_cache.cover_image_url),
                            raw = CASE WHEN EXCLUDED.raw = '{}'::jsonb THEN psn_catalog_cache.raw ELSE EXCLUDED.raw END,
                            fetched_at = now()
                        """,
                        (
                            product.np_title_id,
                            game_id,
                            product.product_id,
                            product.cover_image_url,
                            Jsonb(dict(product.raw)),
                        ),
                    )
                    if product.cover_image_url:
                        covers_cached += 1
        return games_created, covers_cached

    async def game_exists(self, game_id: str) -> bool:
        """Return whether ``game_id`` names a game in the shared canonical catalog."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT 1 FROM games WHERE game_id = %s", (game_id,))
            return await cur.fetchone() is not None

    async def game_ids_for_store_ids(self, store_ids: Sequence[str]) -> dict[str, str]:
        """Resolve PSN universal-search result ids to the games the catalog already holds for them.

        Answers "is this store hit already in the catalog" for a whole page of hits in one round trip.
        Only a positive answer means anything: ``store_product_id`` is populated solely by
        ``POST /catalog/backfill`` and was added to an already-in-use table by ``0029``, so a missing entry
        is as likely to mean "no walk has covered this title" as "the catalog has never seen this game".

        Three id spaces are tried in a fixed order, and the order is the evidence. ``game_concepts
        .concept_id`` is the primary key, is populated for every row, and is what a ``MobileGames`` hit's
        own id is, so it goes first. ``game_concepts.product_id`` is neither unique nor a safe merge key --
        ``0001_initial.sql`` records that Sony reuses one product id across genuinely different games --
        so it is a fallback, ordered to make the pick deterministic rather than arbitrary.
        ``psn_catalog_cache.store_product_id`` is last because it is the sparsest.

        :param store_ids: Result ids as :class:`~curator.psn.models.GameSearchResult` reports them.
        :returns: ``{store_id: game_id}``, carrying only the ids that resolved.
        """
        if not store_ids:
            return {}

        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT candidate.store_id,
                       COALESCE(
                           (SELECT gc.game_id FROM game_concepts gc WHERE gc.concept_id = candidate.store_id),
                           (
                               SELECT gc.game_id FROM game_concepts gc
                               WHERE gc.product_id = candidate.store_id
                               ORDER BY gc.concept_id LIMIT 1
                           ),
                           (
                               SELECT pcc.game_id FROM psn_catalog_cache pcc
                               WHERE pcc.store_product_id = candidate.store_id AND pcc.game_id IS NOT NULL
                               ORDER BY pcc.title_id LIMIT 1
                           )
                       ) AS game_id
                FROM unnest(%s::text[]) AS candidate(store_id)
                """,
                (list(store_ids),),
            )
            rows = await cur.fetchall()
        return {str(row[0]): str(row[1]) for row in rows if row[1] is not None}

    async def admit_store_game(self, *, concept_id: str, name: str, product_id: str | None = None) -> tuple[str, bool]:
        """Admit a PlayStation Store title to the shared catalog, and return the game it now maps to.

        Idempotent by concept id first and normalized title second, so two users admitting the same title
        -- or one user admitting a title a library refresh has already ingested -- converge on one game
        rather than forking the catalog. The advisory lock covers the read-then-insert on ``games``, which
        has an index on ``normalized_title`` but no unique constraint; it takes the exact classid+key pair
        ``Functions``' ``UpsertGameAsync`` takes over the same key (see :data:`GAME_UPSERT_ADVISORY_LOCK_CLASS`
        on why the classid has to match, not just the key), so the two writers cannot interleave into a
        duplicate.

        ``game_enrichment`` gets a bare row, leaving ``rawg_attempted_at`` NULL. That is the "never
        reached, still eligible" state of the pair ``AGENTS/Curator.md`` documents, and
        ``EnrichmentRunProcessor`` unions ``GetGameIdsNeverAskedOfRawgAsync`` (``rawg_attempted_at IS
        NULL``) into its candidate set, so the row makes the game reachable by the catalog-wide pass
        instead of stranding it.

        **No ``psn_catalog_cache`` row is written**, because a search hit carries no npTitleId and that
        table is keyed by one -- see ``AGENTS/Curator.md``.

        :param concept_id: The hit's PSN concept id.
        :param name: The title exactly as PSN published it; also the source of ``normalized_title``.
        :param product_id: The concept's current ``defaultProduct`` id, when it published one.
        :returns: ``(game_id, created)`` -- ``created`` distinguishes a newly admitted game from one the
            catalog already held.
        """
        normalized_title = name.strip().lower()
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                (GAME_UPSERT_ADVISORY_LOCK_CLASS, normalized_title),
            )

            await cur.execute("SELECT game_id FROM game_concepts WHERE concept_id = %s", (concept_id,))
            row = await cur.fetchone()
            if row is not None:
                return str(row[0]), False

            await cur.execute("SELECT game_id FROM games WHERE normalized_title = %s", (normalized_title,))
            row = await cur.fetchone()
            created = row is None
            if row is None:
                await cur.execute(
                    "INSERT INTO games (canonical_title, normalized_title) VALUES (%s, %s) RETURNING game_id",
                    (name.strip(), normalized_title),
                )
                row = await cur.fetchone()
                assert row is not None
            game_id = str(row[0])

            await cur.execute(
                """
                INSERT INTO game_concepts (concept_id, game_id, product_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (concept_id) DO NOTHING
                """,
                (concept_id, game_id, product_id),
            )
            await cur.execute(
                "INSERT INTO game_enrichment (game_id) VALUES (%s) ON CONFLICT (game_id) DO NOTHING",
                (game_id,),
            )
        return game_id, created

    async def title_id_for_game(self, game_id: str) -> str | None:
        """Return the PSN npTitleId the catalog holds for a game, or ``None`` if it holds none.

        ``psn_catalog_cache`` is keyed on ``title_id`` and a concept can carry several editions, so the
        most recently fetched row wins rather than an arbitrary one.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT title_id FROM psn_catalog_cache WHERE game_id = %s ORDER BY fetched_at DESC LIMIT 1",
                (game_id,),
            )
            row = await cur.fetchone()
        return str(row[0]) if row is not None else None

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
