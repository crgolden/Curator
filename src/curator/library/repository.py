"""Repository for the per-user library aggregate: ``library_entries``.

Same shape as :class:`curator.persistence.repository.Repository`: backed by a shared
:class:`~psycopg_pool.AsyncConnectionPool`, raw parameterized SQL, frozen dataclass results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from psycopg import AsyncCursor
from psycopg_pool import AsyncConnectionPool

LibrarySortField = Literal["title", "category", "rawg_rating", "opencritic_rating", "psn_rating", "percent_completed"]

_SORT_COLUMNS: dict[str, str] = {
    "title": "g.canonical_title",
    "category": "gen.name",
    "rawg_rating": "ge.critical_score",
    "opencritic_rating": "ge.oc_score",
    "psn_rating": "ge.psn_rating",
    "percent_completed": "le.trophy_percent_completed",
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
    is_active: bool = True
    np_communication_id: str | None = None
    percent_completed: int | None = None
    source: str = "psn"
    cover_image_url: str | None = None


@dataclass(frozen=True, slots=True)
class ContinuationGame:
    """One game to resume enriching in a library-refresh continuation job -- see
    ``curator.app._library_refresh_continuation_handler``."""

    game_id: str
    title: str
    product_id: str | None
    title_id: str | None
    native_ps5: bool


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
    title_id: str | None


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
        title_id: str | None,
        is_active: bool = True,
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
        :param title_id: The winning edition's PSN store/content title id (npTitleId), used for
            official-PSN-catalog enrichment lookups.
        :param is_active: Whether this user can still play the game. Refreshed on every build, so a
            lapsed PS Plus title flips to ``False`` here instead of being stranded as permanently owned
            (which is what happened while canonicalization dropped inactive entitlements outright).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO library_entries (
                    identity_sub, game_id, native_ps5, ps4_eligible, owned_edition,
                    winning_entitlement_id, product_id, title_id, is_active, source, last_seen_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'psn', now())
                ON CONFLICT (identity_sub, game_id) DO UPDATE SET
                    native_ps5 = EXCLUDED.native_ps5,
                    ps4_eligible = EXCLUDED.ps4_eligible,
                    owned_edition = EXCLUDED.owned_edition,
                    winning_entitlement_id = EXCLUDED.winning_entitlement_id,
                    product_id = EXCLUDED.product_id,
                    title_id = EXCLUDED.title_id,
                    is_active = EXCLUDED.is_active,
                    source = 'psn',
                    last_seen_at = now()
                """,
                (
                    identity_sub,
                    game_id,
                    native_ps5,
                    ps4_eligible,
                    owned_edition,
                    winning_entitlement_id,
                    product_id,
                    title_id,
                    is_active,
                ),
            )
            await self._sync_entry_platforms(
                cur, identity_sub, game_id, native_ps5=native_ps5, ps4_eligible=ps4_eligible
            )

    @staticmethod
    async def _sync_entry_platforms(
        cur: AsyncCursor[Any], identity_sub: str, game_id: str, *, native_ps5: bool, ps4_eligible: bool
    ) -> None:
        """Reconcile ``library_entry_platforms`` to match the entry's boolean pair.

        :param native_ps5: Whether the entry owns the PS5 platform.
        :param ps4_eligible: Whether the entry owns the PS4 platform.
        """
        platforms = [platform for platform, owned in (("PS5", native_ps5), ("PS4", ps4_eligible)) if owned]
        await cur.execute(
            """
            DELETE FROM library_entry_platforms
            WHERE identity_sub = %s AND game_id = %s AND NOT (platform = ANY(%s))
            """,
            (identity_sub, game_id, platforms),
        )
        if platforms:
            await cur.execute(
                """
                INSERT INTO library_entry_platforms (identity_sub, game_id, platform)
                SELECT %s, %s, unnest(%s::text[])
                ON CONFLICT DO NOTHING
                """,
                (identity_sub, game_id, platforms),
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

    async def get_unmatched_game_ids(self, identity_sub: str, game_ids: list[str]) -> list[str]:
        """Return the subset of ``game_ids`` (already this user's ``library_entries`` rows) with no
        persisted trophy-title match yet (``np_communication_id IS NULL``).

        The delta check :class:`~curator.library.library_build_orchestrator.LibraryBuildOrchestrator`'s
        ``match_trophies`` stage uses, mirroring :meth:`~curator.enrichment.repository.EnrichmentRepository
        .get_unenriched_game_ids` -- so a library rebuild only spends PSN trophy-API calls on games not
        already resolved (successfully or not; see ``trophy_match_attempted_at`` in
        ``0014_library_entries_trophy_match.sql``).

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :param game_ids: Candidate ``games.game_id`` values (e.g. this run's just-canonicalized library).
        """
        if not game_ids:
            return []
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT game_id FROM library_entries
                WHERE identity_sub = %s AND game_id = ANY(%s) AND np_communication_id IS NULL
                """,
                (identity_sub, game_ids),
            )
            rows = await cur.fetchall()
        return [str(row[0]) for row in rows]

    async def set_trophy_match(
        self,
        identity_sub: str,
        game_id: str,
        *,
        np_communication_id: str | None,
        method: str | None,
        percent_completed: int | None = None,
    ) -> None:
        """Persist one game's matched PSN trophy-title identity and completion for a user.

        Always stamps ``trophy_match_attempted_at``, even when ``np_communication_id`` is ``None`` -- a
        title with genuinely no trophy set (an app, an F2P title) must not be re-attempted on every future
        refresh forever just because it has no match to show for it.

        ``trophy_progress_fetched_at`` is stamped only when a percentage is actually supplied, so it means
        "this number was true as of then" rather than "we last looked". A match attempt that found nothing
        leaves it null.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :param game_id: The shared ``games.game_id`` this entry resolves to.
        :param np_communication_id: The matched trophy title's id, or ``None`` if no confident match found.
        :param method: ``"exact"`` (PS4 title-id lookup) or ``"fuzzy"`` (name similarity); ``None`` iff
            ``np_communication_id`` is also ``None``.
        :param percent_completed: The matched title's completion percentage, if known.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE library_entries
                SET np_communication_id = %s,
                    trophy_match_method = %s,
                    trophy_match_attempted_at = now(),
                    trophy_percent_completed = %s,
                    trophy_progress_fetched_at = CASE WHEN %s::smallint IS NULL
                        THEN trophy_progress_fetched_at ELSE now() END
                WHERE identity_sub = %s AND game_id = %s
                """,
                (np_communication_id, method, percent_completed, percent_completed, identity_sub, game_id),
            )

    async def refresh_trophy_progress(self, identity_sub: str, progress_by_np_id: dict[str, int]) -> int:
        """Refresh stored completion percentages for games already matched to a trophy title.

        Separate from :meth:`set_trophy_match` because the two happen on very different cadences: identity
        is resolved once per game and never revisited (that is what ``trophy_match_attempted_at`` protects),
        while progress changes every time the user plays. A refresh that only touched newly-matched games
        would leave an established library's percentages frozen at whatever they were the day each game
        was first seen.

        Keyed by ``np_communication_id`` rather than ``game_id`` because that is what a
        ``trophy_titles()`` response actually carries -- no second lookup needed to map it back.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :param progress_by_np_id: ``{np_communication_id: percent_completed}``.
        :returns: The number of rows updated.
        """
        if not progress_by_np_id:
            return 0
        updated = 0
        async with self._pool.connection() as conn, conn.cursor() as cur:
            for np_communication_id, percent in progress_by_np_id.items():
                await cur.execute(
                    """
                    UPDATE library_entries
                    SET trophy_percent_completed = %s, trophy_progress_fetched_at = now()
                    WHERE identity_sub = %s AND np_communication_id = %s
                    """,
                    (percent, identity_sub, np_communication_id),
                )
                updated += cur.rowcount
        return updated

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

    async def list_entries(self, identity_sub: str) -> list[LibraryEntry]:
        """Return every game a user owns, per their most recent library build.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT identity_sub, game_id, native_ps5, ps4_eligible, owned_edition,
                       winning_entitlement_id, product_id, title_id
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
                title_id=row[7],
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

        ``cover_image_url`` uses the same two-source COALESCE fallback
        ``CollectionsRepository.list_definition_items`` already uses (a real PSN store cover from
        ``psn_catalog_cache`` first, entitlement-ingestion artwork second) -- as a scalar subquery, not a
        ``JOIN``, so it can't multiply rows or skew ``total``. Unlike that method this one is already
        scoped to one ``identity_sub`` via ``le``, so the lookup needs no self-join to reach an "any
        owner's" row first.

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
                       COALESCE(ge.rawg_enriched, false), COALESCE(ge.opencritic_enriched, false),
                       le.is_active, le.np_communication_id, le.trophy_percent_completed, le.source,
                       COALESCE(
                           (SELECT pcc.cover_image_url FROM psn_catalog_cache pcc
                            WHERE pcc.title_id = le.title_id AND pcc.cover_image_url IS NOT NULL LIMIT 1),
                           (
                               SELECT COALESCE(es.title_image_url, es.game_icon_url, es.concept_icon_url)
                               FROM game_concepts gc
                               JOIN entitlement_snapshots es ON es.concept_id = gc.concept_id
                               WHERE gc.game_id = g.game_id
                                 AND COALESCE(es.title_image_url, es.game_icon_url, es.concept_icon_url) IS NOT NULL
                               LIMIT 1
                           )
                       ) AS cover_image_url
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
            )
            for row in rows
        ]
        return games, total

    async def get_games_for_continuation(self, identity_sub: str, game_ids: list[str]) -> list[ContinuationGame]:
        """Look up title/product id/platform for a subset of a user's library, by game id.

        Used to resume enrichment after a rate limit (``curator.app._library_refresh_continuation_handler``)
        without re-ingesting/re-canonicalizing from PSN -- these games are already in ``library_entries``
        from the run that got rate-limited; only the fields
        ``curator.library.library_build_orchestrator.enrich_games`` needs are re-read here.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :param game_ids: The subset of the user's owned game ids to look up.
        :returns: One :class:`ContinuationGame` per matching row, in no particular order -- callers that
            need to preserve ``game_ids``' order (so a subsequent rate limit's ``remaining_game_ids``
            reflects the same resume order) should re-sort by it themselves.
        """
        if not game_ids:
            return []
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT le.game_id, g.canonical_title, le.product_id, le.title_id, le.native_ps5
                FROM library_entries le
                JOIN games g ON g.game_id = le.game_id
                WHERE le.identity_sub = %s AND le.game_id = ANY(%s)
                """,
                (identity_sub, game_ids),
            )
            rows = await cur.fetchall()
        return [
            ContinuationGame(game_id=str(row[0]), title=row[1], product_id=row[2], title_id=row[3], native_ps5=row[4])
            for row in rows
        ]

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
