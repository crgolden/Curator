"""Repository for the catalog aggregate: shared games/game_concepts/game_name_overrides, the per-user
ingestion layer (entitlement_pulls/entitlement_snapshots), the canonicalization-rule tables
(exclusion_rules/franchise_rules/edition_ranks), and global_exclusions.

Same shape as :class:`curator.persistence.repository.Repository`: backed by a shared
:class:`~psycopg_pool.AsyncConnectionPool`, raw parameterized SQL, frozen dataclass results.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from psycopg_pool import AsyncConnectionPool

from curator.catalog.canonicalization_service import CanonicalGame, EntitlementSnapshot
from curator.catalog.exclusion_rules import ExclusionRule
from curator.catalog.franchise_assigner import FranchiseRule, assign_franchise
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
            assert count_row is not None  # COUNT(*) always returns exactly one row
            total = count_row[0]

            await cur.execute(
                f"""
                SELECT g.game_id, g.canonical_title, g.franchise, gen.name, ge.aaa_tier,
                       (
                           SELECT pcc.cover_image_url FROM psn_catalog_cache pcc
                           WHERE pcc.game_id = g.game_id AND pcc.cover_image_url IS NOT NULL LIMIT 1
                       ) AS cover_image_url,
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
                """
                SELECT g.game_id, g.canonical_title, g.franchise, gen.name, ge.aaa_tier,
                       COALESCE(
                           (
                               SELECT pcc.cover_image_url FROM psn_catalog_cache pcc
                               WHERE pcc.game_id = g.game_id AND pcc.cover_image_url IS NOT NULL LIMIT 1
                           ),
                           (
                               SELECT COALESCE(es.title_image_url, es.concept_icon_url, es.game_icon_url)
                               FROM entitlement_snapshots es
                               JOIN library_entries le ON le.title_id = es.title_id
                               WHERE le.game_id = g.game_id
                                 AND COALESCE(es.title_image_url, es.concept_icon_url, es.game_icon_url) IS NOT NULL
                               LIMIT 1
                           )
                       ) AS cover_image_url,
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

    async def list_exclusion_rules(self) -> list[ExclusionRule]:
        """Return every exclusion rule."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT rule_id, rule_type, pattern FROM exclusion_rules")
            rows = await cur.fetchall()
        return [ExclusionRule(rule_id=str(row[0]), rule_type=row[1], pattern=row[2]) for row in rows]

    async def list_franchise_rules(self) -> list[FranchiseRule]:
        """Return every franchise-assignment rule."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT rule_id, pattern, franchise, priority FROM franchise_rules")
            rows = await cur.fetchall()
        return [FranchiseRule(rule_id=str(row[0]), pattern=row[1], franchise=row[2], priority=row[3]) for row in rows]

    async def list_all_game_ids_and_titles(self) -> list[tuple[str, str]]:
        """Return every game's ``(game_id, canonical_title)``, for a catalog-wide admin pass."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT game_id, canonical_title FROM games")
            rows = await cur.fetchall()
        return [(str(row[0]), row[1]) for row in rows]

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
                    assert row is not None  # guaranteed by RETURNING game_id above
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

    async def reclassify_franchise(self, rules: list[FranchiseRule]) -> int:
        """Recompute every game's franchise against the current ``franchise_rules``, updating only the
        rows whose value actually changes.

        Franchise assignment is pure title-regex matching -- no external API dependency -- so this can
        run for every game in the catalog regardless of enrichment status, unlike genre/tier
        reclassification which needs already-resolved publisher/developer or genre-tag data.

        :param rules: Every franchise-assignment rule (see :meth:`list_franchise_rules`).
        :returns: The number of games whose franchise changed.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT game_id, canonical_title, franchise FROM games")
            rows = await cur.fetchall()

            updated = 0
            for game_id, canonical_title, current_franchise in rows:
                new_franchise = assign_franchise(canonical_title, rules) or None
                if new_franchise != current_franchise:
                    await cur.execute(
                        "UPDATE games SET franchise = %s, updated_at = now() WHERE game_id = %s",
                        (new_franchise, game_id),
                    )
                    updated += 1
        return updated

    async def get_franchise_rules_fingerprint(self) -> str | None:
        """Return the ``franchise_rules`` content fingerprint as of the last completed
        :meth:`reclassify_franchise` pass, or ``None`` if that pass has never run (see
        ``db/migrations/0016_curation_rule_pass_state.sql``)."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT rules_fingerprint FROM curation_rule_pass_state WHERE pass_name = 'franchise_reclassification'"
            )
            row = await cur.fetchone()
        return row[0] if row is not None else None

    async def set_franchise_rules_fingerprint(self, fingerprint: str) -> None:
        """Record ``fingerprint`` as the ``franchise_rules`` content as of the pass that just ran."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO curation_rule_pass_state (pass_name, rules_fingerprint)
                VALUES ('franchise_reclassification', %s)
                ON CONFLICT (pass_name) DO UPDATE SET
                    rules_fingerprint = EXCLUDED.rules_fingerprint, last_ran_at = now()
                """,
                (fingerprint,),
            )

    async def get_edition_ranks(self) -> dict[str, int]:
        """Return the edition-keyword -> rank mapping."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT keyword, rank FROM edition_ranks")
            rows = await cur.fetchall()
        return {row[0]: row[1] for row in rows}

    async def get_name_overrides(self) -> dict[str, str]:
        """Return the concept-id -> corrected-display-name mapping."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT concept_id, override_name FROM game_name_overrides")
            rows = await cur.fetchall()
        return {row[0]: row[1] for row in rows}

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

    async def get_globally_excluded_concept_ids(self) -> set[str]:
        """Return every permanently-excluded concept id."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT concept_id FROM global_exclusions")
            rows = await cur.fetchall()
        return {row[0] for row in rows}

    async def exclude_globally(self, concept_id: str, reason: str) -> None:
        """Permanently exclude a concept id from every future canonicalization run.

        :param concept_id: The PSN concept id to exclude.
        :param reason: Why it's being excluded (for the audit trail).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO global_exclusions (concept_id, reason)
                VALUES (%s, %s)
                ON CONFLICT (concept_id) DO UPDATE SET reason = EXCLUDED.reason, excluded_at = now()
                """,
                (concept_id, reason),
            )

    async def record_pull(
        self,
        identity_sub: str,
        source: str,
        snapshots: list[EntitlementSnapshot],
        raw: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        """Record one ingestion pull: one ``entitlement_pulls`` row plus one ``entitlement_snapshots`` row
        per entry.

        :param identity_sub: The Curator user id (Identity's ``sub``) this pull belongs to.
        :param source: ``"curator-live"`` or ``"manual-json-import"``.
        :param snapshots: The raw entitlement rows this pull captured.
        :param raw: Optional per-entitlement raw JSON payloads, keyed by ``entitlement_id``, stored
            verbatim alongside the extracted columns so a downstream extraction bug never loses information.
        :returns: The new pull's id.
        """
        raw = raw or {}
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO entitlement_pulls (identity_sub, source, entry_count)
                VALUES (%s, %s, %s)
                RETURNING pull_id
                """,
                (identity_sub, source, len(snapshots)),
            )
            row = await cur.fetchone()
            assert row is not None  # guaranteed by RETURNING pull_id above
            pull_id = str(row[0])

            for snapshot in snapshots:
                await cur.execute(
                    """
                    INSERT INTO entitlement_snapshots (
                        pull_id, entitlement_id, concept_id, product_id, sku_id, title_id,
                        game_meta_name, concept_meta_name, title_meta_name, package_type, active,
                        active_date, title_image_url, game_icon_url, concept_icon_url, is_game,
                        platform_ids, raw
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        pull_id,
                        snapshot.entitlement_id,
                        snapshot.concept_id,
                        snapshot.product_id,
                        snapshot.sku_id,
                        snapshot.title_id,
                        snapshot.game_meta_name,
                        snapshot.concept_meta_name,
                        snapshot.title_meta_name,
                        snapshot.package_type,
                        snapshot.active,
                        snapshot.active_date,
                        snapshot.title_image_url,
                        snapshot.game_icon_url,
                        snapshot.concept_icon_url,
                        snapshot.is_game,
                        list(snapshot.platform_ids),
                        json.dumps(raw.get(snapshot.entitlement_id, {})),
                    ),
                )
        return pull_id

    async def upsert_game(self, game: CanonicalGame) -> str:
        """Merge one canonical game into the shared catalog.

        Matches by known concept id first (most reliable -- a concept never moves to a different game),
        then by normalized title, else inserts a new row. Every concept id in ``game.concept_ids`` gets
        (or keeps) a ``game_concepts`` row pointing at the resolved game.

        :param game: The canonical game to merge in (from
            :func:`~curator.catalog.canonicalization_service.canonicalize`).
        :returns: The resolved (existing or newly created) game's id.
        """
        normalized_title = game.canonical_title.strip().lower()
        async with self._pool.connection() as conn, conn.cursor() as cur:
            game_id: str | None = None
            if game.concept_ids:
                await cur.execute(
                    "SELECT game_id FROM game_concepts WHERE concept_id = ANY(%s) LIMIT 1",
                    (list(game.concept_ids),),
                )
                row = await cur.fetchone()
                if row:
                    game_id = str(row[0])

            if game_id is None:
                await cur.execute("SELECT game_id FROM games WHERE normalized_title = %s", (normalized_title,))
                row = await cur.fetchone()
                if row:
                    game_id = str(row[0])

            if game_id is None:
                await cur.execute(
                    """
                    INSERT INTO games (canonical_title, normalized_title, franchise)
                    VALUES (%s, %s, %s)
                    RETURNING game_id
                    """,
                    (game.canonical_title, normalized_title, game.franchise or None),
                )
                row = await cur.fetchone()
                assert row is not None  # guaranteed by RETURNING game_id above
                game_id = str(row[0])
            else:
                await cur.execute(
                    "UPDATE games SET canonical_title = %s, franchise = %s, updated_at = now() WHERE game_id = %s",
                    (game.canonical_title, game.franchise or None, game_id),
                )

            for concept_id in game.concept_ids:
                await cur.execute(
                    """
                    INSERT INTO game_concepts (concept_id, game_id, product_id)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (concept_id) DO UPDATE SET
                        game_id = EXCLUDED.game_id,
                        product_id = EXCLUDED.product_id
                    """,
                    (concept_id, game_id, game.product_id),
                )

        return game_id
