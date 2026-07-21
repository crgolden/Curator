"""Repository for the per-user library aggregate: ``library_entries``.

Same shape as :class:`curator.persistence.repository.Repository`: backed by a shared
:class:`~psycopg_pool.AsyncConnectionPool`, raw parameterized SQL, frozen dataclass results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from psycopg_pool import AsyncConnectionPool

LibrarySortField = Literal["title", "category", "rawg_rating", "opencritic_rating", "psn_rating"]

_SORT_COLUMNS: dict[str, str] = {
    "title": "g.canonical_title",
    "category": "gen.name",
    "rawg_rating": "ge.critical_score",
    "opencritic_rating": "ge.oc_score",
    "psn_rating": "ge.psn_rating",
}


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


@dataclass(frozen=True, slots=True)
class LibraryEntry:
    """One row from ``library_entries``: a user's derived ownership of one game."""

    identity_sub: str
    game_id: str
    native_ps5: bool
    ps4_eligible: bool
    owned_edition: str | None
    winning_entitlement_id: str | None
    product_id: str | None


class LibraryRepository:
    """DAO over ``library_entries``.

    :param pool: The shared connection pool.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def upsert_entry(
        self,
        identity_sub: str,
        game_id: str,
        *,
        native_ps5: bool,
        ps4_eligible: bool,
        owned_edition: str | None,
        winning_entitlement_id: str | None,
        product_id: str | None,
    ) -> None:
        """Record (or refresh) a user's ownership of one game.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :param game_id: The shared ``games.game_id`` this entry resolves to.
        :param native_ps5: Whether the winning edition is PS5-native.
        :param ps4_eligible: Whether a PS4 edition is playable.
        :param owned_edition: The winning edition's display name/label, if tracked separately from the
            game's canonical title.
        :param winning_entitlement_id: The entitlement id that won the edition tiebreak.
        :param product_id: The winning edition's PSN product id.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO library_entries (
                    identity_sub, game_id, native_ps5, ps4_eligible, owned_edition,
                    winning_entitlement_id, product_id, last_seen_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (identity_sub, game_id) DO UPDATE SET
                    native_ps5 = EXCLUDED.native_ps5,
                    ps4_eligible = EXCLUDED.ps4_eligible,
                    owned_edition = EXCLUDED.owned_edition,
                    winning_entitlement_id = EXCLUDED.winning_entitlement_id,
                    product_id = EXCLUDED.product_id,
                    last_seen_at = now()
                """,
                (identity_sub, game_id, native_ps5, ps4_eligible, owned_edition, winning_entitlement_id, product_id),
            )

    async def list_entries(self, identity_sub: str) -> list[LibraryEntry]:
        """Return every game a user owns, per their most recent library build.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT identity_sub, game_id, native_ps5, ps4_eligible, owned_edition,
                       winning_entitlement_id, product_id
                FROM library_entries WHERE identity_sub = %s
                """,
                (identity_sub,),
            )
            rows = await cur.fetchall()
        return [
            LibraryEntry(
                identity_sub=str(row[0]),
                game_id=str(row[1]),
                native_ps5=row[2],
                ps4_eligible=row[3],
                owned_edition=row[4],
                winning_entitlement_id=row[5],
                product_id=row[6],
            )
            for row in rows
        ]

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
            assert count_row is not None  # COUNT(*) always returns exactly one row
            total = count_row[0]

            await cur.execute(
                f"""
                SELECT g.game_id, g.canonical_title, gen.name, ge.critical_score, ge.oc_score,
                       ge.psn_rating, le.product_id,
                       COALESCE(ge.rawg_enriched, false), COALESCE(ge.opencritic_enriched, false)
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
