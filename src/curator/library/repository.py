"""Repository for the per-user library aggregate: ``library_entries``.

Same shape as :class:`curator.persistence.repository.Repository`: backed by a shared
:class:`~psycopg_pool.AsyncConnectionPool`, raw parameterized SQL, frozen dataclass results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncCursor
from psycopg_pool import AsyncConnectionPool

from curator.catalog.cover_art import SQUARE_COVER_ART_SQL

LibrarySortField = Literal["title", "category", "rawg_rating", "opencritic_rating", "psn_rating", "percent_completed"]

_SORT_COLUMNS: dict[str, str] = {
    "title": "g.canonical_title",
    "category": "gen.name",
    "rawg_rating": "ge.critical_score",
    "opencritic_rating": "ge.oc_score",
    "psn_rating": "ge.psn_rating",
    "percent_completed": "le.trophy_percent_completed",
}

_OWNED_PLATFORMS_SQL = """(
                           SELECT COALESCE(array_agg(p.platform_id ORDER BY p.sort_order), ARRAY[]::text[])
                           FROM library_entry_platforms lep
                           JOIN platforms p ON p.platform_id = lep.platform
                           WHERE lep.identity_sub = le.identity_sub AND lep.game_id = le.game_id
                       )"""
"""Correlated scalar subquery yielding one entry's platforms, ordered by ``platforms.sort_order``.

The outer query must alias ``library_entries`` as ``le``. Scalar rather than a join so a game owned on
three platforms stays one row and cannot inflate a ``COUNT(*)`` taken over the same ``FROM`` clause.
``array_agg`` returns ``NULL`` over no rows, so the empty case is coalesced to an empty array rather
than reaching the caller as ``None``.
"""


@dataclass(frozen=True, slots=True)
class LibraryGameView:
    """One row of a user's library, joined with its enrichment status -- backs ``GET /library``'s
    rating/category columns."""

    game_id: str
    title: str
    category: str | None
    rawg_rating: float | None
    opencritic_rating: float | None
    psn_rating: float | None
    psn_product_id: str | None
    rawg_enriched: bool
    opencritic_enriched: bool
    is_active: bool = True
    np_communication_id: str | None = None
    percent_completed: int | None = None
    source: str = "psn"
    cover_image_url: str | None = None
    platforms: tuple[str, ...] = ()


class LibraryRepository:
    """DAO over ``library_entries``.

    :param pool: The shared connection pool.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    @staticmethod
    async def _sync_entry_platforms(
        cur: AsyncCursor[Any],
        identity_sub: str,
        game_id: str,
        *,
        native_ps5: bool,
        ps4_eligible: bool,
        platforms: tuple[str, ...] = (),
    ) -> None:
        """Reconcile ``library_entry_platforms`` to match the entry's boolean pair, plus any extra platforms.

        :param native_ps5: Whether the entry owns the PS5 platform.
        :param ps4_eligible: Whether the entry owns the PS4 platform.
        :param platforms: Extra platforms to union in beyond the PS5/PS4 pair -- already-present values
            are not duplicated.
        """
        owned_platforms = [platform for platform, owned in (("PS5", native_ps5), ("PS4", ps4_eligible)) if owned]
        for platform in platforms:
            if platform not in owned_platforms:
                owned_platforms.append(platform)
        await cur.execute(
            """
            DELETE FROM library_entry_platforms
            WHERE identity_sub = %s AND game_id = %s AND NOT (platform = ANY(%s::text[]))
            """,
            (identity_sub, game_id, owned_platforms),
        )
        if owned_platforms:
            await cur.execute(
                """
                INSERT INTO library_entry_platforms (identity_sub, game_id, platform)
                SELECT %s, %s, unnest(%s::text[])
                ON CONFLICT DO NOTHING
                """,
                (identity_sub, game_id, owned_platforms),
            )

    async def upsert_manual_entry(
        self, identity_sub: str, game_id: str, *, native_ps5: bool, ps4_eligible: bool, owned_edition: str | None
    ) -> None:
        """Record a game the user owns that PSN has no entitlement for -- a physical disc, typically."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO library_entries (
                    identity_sub, game_id, native_ps5, ps4_eligible, owned_edition, is_active, source, last_seen_at
                )
                VALUES (%s, %s, %s, %s, %s, true, 'manual', now())
                ON CONFLICT (identity_sub, game_id) DO UPDATE SET
                    native_ps5 = EXCLUDED.native_ps5,
                    ps4_eligible = EXCLUDED.ps4_eligible,
                    owned_edition = EXCLUDED.owned_edition,
                    last_seen_at = now()
                WHERE library_entries.source = 'manual'
                """,
                (identity_sub, game_id, native_ps5, ps4_eligible, owned_edition),
            )
            if cur.rowcount:
                await self._sync_entry_platforms(
                    cur, identity_sub, game_id, native_ps5=native_ps5, ps4_eligible=ps4_eligible
                )

    async def delete_manual_entry(self, identity_sub: str, game_id: str) -> bool:
        """Remove a manually-added game, never a PSN-sourced one.

        :returns: ``True`` if a row was removed, ``False`` if there was no manual entry for that game.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM library_entries WHERE identity_sub = %s AND game_id = %s AND source = 'manual'",
                (identity_sub, game_id),
            )
            return bool(cur.rowcount)

    async def clear_trophy_progress(self, identity_sub: str) -> int:
        """Erase every stored trophy percentage for a user, for use when they disable ``harvest_trophies``.

        Turning the preference off has to remove what is already stored, not merely stop refreshing it --
        otherwise opting out leaves the data sitting there indefinitely, which is not what a user
        switching a data-collection toggle off reasonably expects. See
        ``0015_library_entries_trophy_progress.sql``.

        The match identity (``np_communication_id``) is deliberately left in place: it is not PSN activity
        data, just a stable id-to-id mapping, and keeping it means re-enabling the preference doesn't have
        to re-run the whole matching pass.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :returns: The number of rows cleared.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE library_entries
                SET trophy_percent_completed = NULL, trophy_progress_fetched_at = NULL
                WHERE identity_sub = %s AND trophy_percent_completed IS NOT NULL
                """,
                (identity_sub,),
            )
            return cur.rowcount

    async def list_entries_with_enrichment(
        self,
        identity_sub: str,
        *,
        search: str | None = None,
        category: str | None = None,
        sort: LibrarySortField = "title",
        sort_dir: str = "asc",
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[LibraryGameView], int]:
        """Return one page of a user's library, joined with its category/ratings/enrichment status,
        for ``GET /library``'s (and ``GET /users/{sub}/library``'s) table -- plus the total count of
        every row matching ``search``/``category``, independent of ``limit``/``offset``.

        ``LEFT JOIN game_enrichment``/``genres`` -- a freshly-ingested-but-not-yet-enriched game has
        no ``game_enrichment`` row yet, and every rating/category field correctly comes back ``None``
        (not enriched yet, not an error); ``rawg_enriched``/``opencritic_enriched`` still default to
        ``False`` via ``COALESCE``.

        ``cover_image_url`` is :data:`~curator.catalog.cover_art.SQUARE_COVER_ART_SQL`, the same
        expression every other cover-returning query uses. ``platforms`` is
        :data:`_OWNED_PLATFORMS_SQL`, ordered by ``platforms.sort_order`` so a multi-platform game reads
        newest-first rather than alphabetically.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :param search: Optional case-insensitive title substring filter.
        :param category: Optional exact-match category (resolved genre name) filter.
        :param sort: Which column to sort by -- looked up through :data:`_SORT_COLUMNS` rather than
            trusted directly, even though the route layer already constrains it to a safe literal.
        :param sort_dir: ``"asc"`` or ``"desc"``; anything else is treated as ``"asc"``.
        :param limit: Page size.
        :param offset: Number of matching rows to skip.
        """
        conditions: list[str] = ["le.identity_sub = %s"]
        params: list[Any] = [identity_sub]
        if search:
            conditions.append("g.canonical_title ILIKE %s")
            params.append(f"%{search}%")
        if category:
            conditions.append("gen.name = %s")
            params.append(category)
        where_clause = " AND ".join(conditions)

        sort_column = _SORT_COLUMNS[sort]
        direction = "DESC" if sort_dir == "desc" else "ASC"

        base_query = f"""
            FROM library_entries le
            JOIN games g ON g.game_id = le.game_id
            LEFT JOIN game_enrichment ge ON ge.game_id = le.game_id
            LEFT JOIN genres gen ON gen.genre_id = ge.genre_id
            WHERE {where_clause}
        """

        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(f"SELECT COUNT(*) {base_query}", tuple(params))
            count_row = await cur.fetchone()
            assert count_row is not None
            total = count_row[0]

            await cur.execute(
                f"""
                SELECT g.game_id, g.canonical_title, gen.name, ge.critical_score, ge.oc_score,
                       ge.psn_rating, le.product_id,
                       COALESCE(ge.rawg_enriched, false), COALESCE(ge.opencritic_enriched, false),
                       le.is_active, le.np_communication_id, le.trophy_percent_completed, le.source,
                       {SQUARE_COVER_ART_SQL} AS cover_image_url,
                       {_OWNED_PLATFORMS_SQL} AS platforms
                {base_query}
                ORDER BY {sort_column} {direction} NULLS LAST, g.canonical_title ASC
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
            rows = await cur.fetchall()

        games = [
            LibraryGameView(
                game_id=str(row[0]),
                title=row[1],
                category=row[2],
                rawg_rating=row[3],
                opencritic_rating=row[4],
                psn_rating=row[5],
                psn_product_id=row[6],
                rawg_enriched=row[7],
                opencritic_enriched=row[8],
                is_active=bool(row[9]),
                np_communication_id=row[10],
                percent_completed=row[11],
                source=row[12],
                cover_image_url=row[13],
                platforms=tuple(row[14] or ()),
            )
            for row in rows
        ]
        return games, total

    async def list_categories(self, identity_sub: str) -> list[str]:
        """Return the distinct, sorted set of categories (resolved genres) present in a user's
        library -- backs the library page's category filter dropdown.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT DISTINCT gen.name
                FROM library_entries le
                JOIN game_enrichment ge ON ge.game_id = le.game_id
                JOIN genres gen ON gen.genre_id = ge.genre_id
                WHERE le.identity_sub = %s AND gen.name IS NOT NULL
                ORDER BY gen.name
                """,
                (identity_sub,),
            )
            rows = await cur.fetchall()
        return [row[0] for row in rows]
