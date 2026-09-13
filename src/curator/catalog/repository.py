"""Repository for the catalog aggregate: shared games/game_concepts/game_name_overrides, the per-user
ingestion layer (entitlement_pulls/entitlement_snapshots), and the canonicalization-rule tables
(franchise_rules/edition_ranks).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from curator.catalog.content_kind import BROWSABLE_KIND_SQL, CONTENT_KINDS, EVERY_KIND, GAME_KIND, ContentKind
from curator.catalog.cover_art import SQUARE_COVER_ART_SQL
from curator.psn.store_client import StoreProduct
from curator.scoring.size_estimation_service import SizeEstimate

CatalogSortField = Literal["title", "price"]

_CATALOG_SORT_COLUMNS: dict[str, str] = {
    "title": "g.canonical_title",
    "price": "price.price_discounted_cents",
}

_PRICE_JOIN_SQL = """
            LEFT JOIN LATERAL (
                SELECT pcc.price_is_free, pcc.price_tied_to_subscription, pcc.price_base_cents,
                       pcc.price_discounted_cents, pcc.price_discount_text, pcc.price_fetched_at
                FROM psn_catalog_cache pcc
                WHERE pcc.game_id = g.game_id AND pcc.price_fetched_at IS NOT NULL
                ORDER BY pcc.price_fetched_at DESC LIMIT 1
            ) price ON true
"""
"""The most recently walked price for an aliased ``games g``; every column NULL when no walk carried one."""

_PRICE_SELECT_SQL = """price.price_is_free, price.price_tied_to_subscription, price.price_base_cents,
                       price.price_discounted_cents, price.price_discount_text, price.price_fetched_at"""


@dataclass(frozen=True, slots=True)
class CatalogPrice:
    """The storefront price the most recent walk recorded for a game (``0062``)."""

    is_free: bool | None
    tied_to_subscription: bool | None
    base_cents: int | None
    discounted_cents: int | None
    discount_text: str | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class GameSummary:
    """One row of ``GET /catalog/games``'s browsing result.

    :param content_kind: ``None`` when the game has never been classified, which browses as a game.
    :param price: ``None`` when no walk has carried a price node for the game.
    """

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
    content_kind: ContentKind | None = None
    price: CatalogPrice | None = None


@dataclass(frozen=True, slots=True)
class PublicCollectionSummary:
    """One public collection containing a game, as ``GET /catalog/games/{gameId}/collections`` lists it."""

    definition_id: str
    name: str
    share_slug: str
    item_count: int
    updated_at: datetime


PUBLIC_COLLECTIONS_CONTAINING_SQL = """
SELECT cd.definition_id, cd.name, cd.share_slug,
       (SELECT count(*) FROM collection_definition_items items WHERE items.definition_id = cd.definition_id),
       cd.updated_at
FROM collection_definitions cd
JOIN collection_definition_items cdi ON cdi.definition_id = cd.definition_id
WHERE cdi.game_id = %s AND cd.visibility = 'public' AND cd.share_slug IS NOT NULL
ORDER BY cd.updated_at DESC, cd.definition_id
LIMIT %s
"""
"""Parameters: ``(game_id, limit)``. ``visibility = 'public'`` and never ``!= 'private'``: an unlisted
collection is reachable by its link and deliberately not listed, and this lands on an indexed page."""

PUBLIC_COLLECTIONS_CONTAINING_COUNT_SQL = """
SELECT count(*)
FROM collection_definitions cd
JOIN collection_definition_items cdi ON cdi.definition_id = cd.definition_id
WHERE cdi.game_id = %s AND cd.visibility = 'public' AND cd.share_slug IS NOT NULL
"""
"""Parameters: ``(game_id,)``."""


def _kind_predicate(kind: str | None) -> tuple[str, list[Any]]:
    """The browsing predicate for a ``kind`` request over an aliased ``games g``.

    :returns: ``(sql, params)``. ``None`` and ``game`` browse unclassified rows as games; ``all`` lifts
        the exclusion; any other kind selects exactly that kind.
    """
    if kind is None or kind == GAME_KIND:
        return BROWSABLE_KIND_SQL, []
    if kind == EVERY_KIND:
        return "", []
    if kind not in CONTENT_KINDS:
        raise ValueError(f"Unknown content kind {kind!r}.")
    return "g.content_kind = %s", [kind]


def _to_price(row: Sequence[Any], start: int) -> CatalogPrice | None:
    fetched_at = row[start + 5]
    if fetched_at is None:
        return None
    return CatalogPrice(
        is_free=row[start],
        tied_to_subscription=row[start + 1],
        base_cents=row[start + 2],
        discounted_cents=row[start + 3],
        discount_text=row[start + 4],
        fetched_at=fetched_at,
    )


@dataclass(frozen=True, slots=True)
class CatalogPage:
    """One page of ``GET /catalog/games`` plus what the caller's own library removed from it.

    ``excluded_owned`` exists so a client never has to infer why a page came back empty. Without it,
    "nothing matches that name" and "you already own every match" are the same empty list, and the only
    way to tell them apart is a second, unfiltered request the client then reasons about -- putting a
    domain rule in the browser. Zero whenever no exclusion was asked for.
    """

    games: list[GameSummary]
    total: int
    excluded_owned: int = 0


GAME_UPSERT_ADVISORY_LOCK_CLASS = 1
"""Must equal ``Functions``' own ``CuratorAdvisoryLocks.GameUpsert`` -- Postgres keeps the single-bigint
and two-int ``pg_advisory_xact_lock`` forms in separate lock spaces, so both the form and this classid have
to match for the two repos to contend for the same lock."""

RESOLVE_STORE_IDS_SQL = """
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
"""
"""The three id spaces a store hit is resolved through, in order. Parameters: ``(store_ids,)``."""

LINK_STORE_CONCEPT_SQL = """
INSERT INTO game_concepts (concept_id, game_id, product_id)
VALUES (%s, %s, %s)
ON CONFLICT (concept_id) DO NOTHING
"""
"""Parameters: ``(concept_id, game_id, product_id)``."""

FILL_STORE_COVER_SQL = (
    "UPDATE games SET store_cover_image_url = %s WHERE game_id = %s AND store_cover_image_url IS NULL"
)
"""Parameters: ``(cover_image_url, game_id)``."""


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
        exclude_owned_by: str | None = None,
        kind: str | None = None,
        sort: CatalogSortField = "title",
        sort_dir: str = "asc",
        limit: int = 50,
        offset: int = 0,
    ) -> CatalogPage:
        """Return a page of the shared game catalog, its total, and what the caller already owns.

        :param search: Optional case-insensitive title substring filter.
        :param franchise: Restrict to this exact franchise, if given.
        :param genre: Restrict to this exact genre name, if given.
        :param aaa_tier: Restrict to this publisher tier, if given.
        :param kind: Which content kinds to browse. ``None`` and ``"game"`` exclude only proven non-games
            (an unclassified row browses as a game); ``"all"`` lifts the exclusion; any other kind selects
            exactly that kind. Applied to the count and the owned count alike, so ``total`` and paging
            describe the same set.
        :param sort: ``"title"`` or ``"price"``, looked up through a closed allowlist. A price sort puts
            games with no recorded price last in either direction.
        :param sort_dir: ``"asc"`` or ``"desc"``; anything else is treated as ``"asc"``.
        :param exclude_owned_by: Drop games this ``identity_sub`` already holds a library entry for,
            whatever its source. The predicate joins the WHERE clause rather than filtering the returned
            page, so ``total`` and every subsequent page describe the same reduced set -- a caller paging
            an unfiltered page and discarding owned rows itself would silently lose the addable games that
            sit behind them. The matches it removed are counted into
            :attr:`CatalogPage.excluded_owned`, so the caller is never left inferring why a page is empty.
        :param limit: Maximum number of rows to return.
        :param offset: Number of matching rows to skip (for pagination).
        """
        conditions: list[str] = []
        params: list[Any] = []
        owned_conditions: list[str] = []
        owned_params: list[Any] = []
        kind_sql, kind_params = _kind_predicate(kind)
        if kind_sql:
            conditions.append(kind_sql)
            params.extend(kind_params)
            owned_conditions.append(kind_sql)
            owned_params.extend(kind_params)
        if search:
            conditions.append("g.canonical_title ILIKE %s")
            params.append(f"%{search}%")
            owned_conditions.append("g.canonical_title ILIKE %s")
            owned_params.append(f"%{search}%")
        if exclude_owned_by is not None:
            ownership = (
                "EXISTS (SELECT 1 FROM library_entries le WHERE le.game_id = g.game_id AND le.identity_sub = %s)"
            )
            conditions.append(f"NOT {ownership}")
            params.append(exclude_owned_by)
            owned_conditions.append(ownership)
            owned_params.append(exclude_owned_by)
        if franchise is not None:
            conditions.append("g.franchise = %s")
            params.append(franchise)
            owned_conditions.append("g.franchise = %s")
            owned_params.append(franchise)
        if genre is not None:
            conditions.append("gen.name = %s")
            params.append(genre)
            owned_conditions.append("gen.name = %s")
            owned_params.append(genre)
        if aaa_tier is not None:
            conditions.append("ge.aaa_tier = %s")
            params.append(aaa_tier)
            owned_conditions.append("ge.aaa_tier = %s")
            owned_params.append(aaa_tier)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        sort_column = _CATALOG_SORT_COLUMNS[sort]
        direction = "DESC" if sort_dir == "desc" else "ASC"

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
                       ge.critical_score, ge.oc_score, ge.psn_rating, g.content_kind,
                       {_PRICE_SELECT_SQL}
                FROM games g
                LEFT JOIN game_enrichment ge ON ge.game_id = g.game_id
                LEFT JOIN genres gen ON gen.genre_id = ge.genre_id
                {_PRICE_JOIN_SQL}
                {where_clause}
                ORDER BY {sort_column} {direction} NULLS LAST, g.canonical_title ASC, g.game_id
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
            rows = await cur.fetchall()

            excluded_owned = 0
            if exclude_owned_by is not None:
                owned_where = f"WHERE {' AND '.join(owned_conditions)}"
                await cur.execute(
                    f"""
                    SELECT COUNT(*)
                    FROM games g
                    LEFT JOIN game_enrichment ge ON ge.game_id = g.game_id
                    LEFT JOIN genres gen ON gen.genre_id = ge.genre_id
                    {owned_where}
                    """,
                    tuple(owned_params),
                )
                owned_row = await cur.fetchone()
                assert owned_row is not None
                excluded_owned = owned_row[0]

        return CatalogPage(
            games=[
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
                    content_kind=row[10],
                    price=_to_price(row, 11),
                )
                for row in rows
            ],
            total=total,
            excluded_owned=excluded_owned,
        )

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
                       ) AS percent_completed,
                       g.content_kind,
                       {_PRICE_SELECT_SQL}
                FROM games g
                LEFT JOIN game_enrichment ge ON ge.game_id = g.game_id
                LEFT JOIN genres gen ON gen.genre_id = ge.genre_id
                {_PRICE_JOIN_SQL}
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
            content_kind=row[11],
            price=_to_price(row, 12),
        )

    async def list_public_collections_containing(
        self, game_id: str, *, limit: int = 20
    ) -> tuple[list[PublicCollectionSummary], int]:
        """Return the public collections that contain a game, newest first, and how many there are.

        Only ``visibility = 'public'`` rows: an unlisted collection is reachable by its share link and
        deliberately not listed, and this answer lands on a page the sitemap hands to search engines.

        :param game_id: The game's id.
        :param limit: The most collections to list; the count is over every match.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(PUBLIC_COLLECTIONS_CONTAINING_SQL, (game_id, limit))
            rows = await cur.fetchall()
            await cur.execute(PUBLIC_COLLECTIONS_CONTAINING_COUNT_SQL, (game_id,))
            count_row = await cur.fetchone()
        total = int(count_row[0]) if count_row is not None else 0
        return [
            PublicCollectionSummary(
                definition_id=str(row[0]),
                name=row[1],
                share_slug=str(row[2]),
                item_count=int(row[3]),
                updated_at=row[4],
            )
            for row in rows
        ], total

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
                    price = product.price
                    await cur.execute(
                        """
                        INSERT INTO psn_catalog_cache
                            (title_id, game_id, store_product_id, cover_image_url, raw, fetched_at,
                             price_is_free, price_tied_to_subscription, price_base_cents,
                             price_discounted_cents, price_discount_text, price_fetched_at)
                        VALUES (%s, %s, %s, %s, %s, now(), %s, %s, %s, %s, %s,
                                CASE WHEN %s THEN now() END)
                        ON CONFLICT (title_id) DO UPDATE SET
                            game_id = EXCLUDED.game_id,
                            store_product_id = EXCLUDED.store_product_id,
                            cover_image_url = COALESCE(EXCLUDED.cover_image_url, psn_catalog_cache.cover_image_url),
                            raw = CASE WHEN EXCLUDED.raw = '{}'::jsonb THEN psn_catalog_cache.raw ELSE EXCLUDED.raw END,
                            fetched_at = now(),
                            price_is_free = COALESCE(EXCLUDED.price_is_free, psn_catalog_cache.price_is_free),
                            price_tied_to_subscription = COALESCE(
                                EXCLUDED.price_tied_to_subscription, psn_catalog_cache.price_tied_to_subscription
                            ),
                            price_base_cents = CASE WHEN EXCLUDED.price_fetched_at IS NOT NULL
                                THEN EXCLUDED.price_base_cents ELSE psn_catalog_cache.price_base_cents END,
                            price_discounted_cents = CASE WHEN EXCLUDED.price_fetched_at IS NOT NULL
                                THEN EXCLUDED.price_discounted_cents ELSE psn_catalog_cache.price_discounted_cents END,
                            price_discount_text = CASE WHEN EXCLUDED.price_fetched_at IS NOT NULL
                                THEN EXCLUDED.price_discount_text ELSE psn_catalog_cache.price_discount_text END,
                            price_fetched_at = COALESCE(EXCLUDED.price_fetched_at, psn_catalog_cache.price_fetched_at)
                        """,
                        (
                            product.np_title_id,
                            game_id,
                            product.product_id,
                            product.cover_image_url,
                            Jsonb(dict(product.raw)),
                            price.is_free if price else None,
                            price.tied_to_subscription if price else None,
                            price.base_cents if price else None,
                            price.discounted_cents if price else None,
                            price.discount_text if price else None,
                            price is not None,
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
            await cur.execute(RESOLVE_STORE_IDS_SQL, (list(store_ids),))
            rows = await cur.fetchall()
        return {str(row[0]): str(row[1]) for row in rows if row[1] is not None}

    async def link_store_concept(
        self, game_id: str, *, concept_id: str, product_id: str | None, cover_image_url: str | None
    ) -> None:
        """Record that a store concept resolves to a game the catalog already holds.

        Used when a search hit resolved through ``product_id`` or ``store_product_id`` only: without the
        ``game_concepts`` link, the next admission of the same concept would fall back to
        ``normalized_title`` and could create a second ``games`` row for a title whose store name
        normalizes differently from the stored ``canonical_title``. The cover fills a hole and never
        overwrites.

        :param game_id: The catalog game the hit resolved to.
        :param concept_id: The hit's PSN concept id.
        :param product_id: The concept's current ``defaultProduct`` id, when it published one.
        :param cover_image_url: The hit's own cover, kept as the fallback artwork.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(LINK_STORE_CONCEPT_SQL, (concept_id, game_id, product_id))
            if cover_image_url is not None:
                await cur.execute(FILL_STORE_COVER_SQL, (cover_image_url, game_id))

    async def admit_store_game(
        self,
        *,
        concept_id: str,
        name: str,
        product_id: str | None = None,
        cover_image_url: str | None = None,
    ) -> tuple[str, bool]:
        """Admit a PlayStation Store title to the shared catalog, and return the game it now maps to.

        Idempotent by concept id first and normalized title second, so two users admitting the same title
        -- or one user admitting a title a library refresh has already ingested -- converge on one game
        rather than forking the catalog. The advisory lock covers the read-then-insert on ``games``, which
        has an index on ``normalized_title`` but no unique constraint; it takes the exact classid+key pair
        ``Functions``' ``UpsertGameAsync`` takes over the same key (see :data:`GAME_UPSERT_ADVISORY_LOCK_CLASS`
        on why the classid has to match, not just the key), so the two writers cannot interleave into a
        duplicate.

        ``game_enrichment`` gets a bare row, leaving ``rawg_attempted_at`` NULL. That is the "never
        reached, still eligible" state of the pair ``AGENTS/REPOS/Curator.md`` documents, and
        ``EnrichmentRunProcessor`` unions ``GetGameIdsNeverAskedOfRawgAsync`` (``rawg_attempted_at IS
        NULL``) into its candidate set, so the row makes the game reachable by the catalog-wide pass
        instead of stranding it.

        **No ``psn_catalog_cache`` row is written**, because a search hit carries no npTitleId and that
        table is keyed by one -- see ``AGENTS/REPOS/Curator.md``. That is why the cover is kept on ``games``
        instead: it is the only identifier a search hit carries all the way through admission, and
        discarding the image the search already returned left a hand-added game with no art forever, since
        nothing running later can recover it.

        An existing game keeps the cover it already has; the fallback only fills a hole. A title the
        catalog gained from a library refresh therefore keeps rendering its entitlement artwork.

        :param concept_id: The hit's PSN concept id.
        :param name: The title exactly as PSN published it; also the source of ``normalized_title``.
        :param product_id: The concept's current ``defaultProduct`` id, when it published one.
        :param cover_image_url: The hit's own cover, kept as the fallback
            :data:`~curator.catalog.cover_art.SQUARE_COVER_ART_SQL` reaches for when PSN carries no
            entitlement artwork -- which is the common case for the back catalogue this feature exists to
            cover.
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
                    """
                    INSERT INTO games (canonical_title, normalized_title, store_cover_image_url)
                    VALUES (%s, %s, %s)
                    RETURNING game_id
                    """,
                    (name.strip(), normalized_title, cover_image_url),
                )
                row = await cur.fetchone()
                assert row is not None
            game_id = str(row[0])

            if cover_image_url is not None:
                await cur.execute(FILL_STORE_COVER_SQL, (cover_image_url, game_id))

            await cur.execute(LINK_STORE_CONCEPT_SQL, (concept_id, game_id, product_id))
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
