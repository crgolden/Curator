"""Repository for the per-user library aggregate: ``library_entries``.

Same shape as :class:`curator.persistence.repository.Repository`: backed by a shared
:class:`~psycopg_pool.AsyncConnectionPool`, raw parameterized SQL, frozen dataclass results.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True, slots=True)
class LibraryGameView:
    """One row of a user's library, joined with its enrichment status -- backs ``GET /library``'s
    per-provider checkmarks."""

    game_id: str
    title: str
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

    async def list_entries_with_enrichment(self, identity_sub: str) -> list[LibraryGameView]:
        """Return every game a user owns with its per-provider enrichment status, for ``GET /library``'s
        checkmark view.

        ``LEFT JOIN game_enrichment`` -- a freshly-ingested-but-not-yet-enriched game has no
        ``game_enrichment`` row yet, and both flags correctly default to ``False`` in that case (not
        enriched yet, not an error).

        :param identity_sub: The Curator user id (Identity's ``sub``).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT g.game_id, g.canonical_title,
                       COALESCE(ge.rawg_enriched, false), COALESCE(ge.opencritic_enriched, false)
                FROM library_entries le
                JOIN games g ON g.game_id = le.game_id
                LEFT JOIN game_enrichment ge ON ge.game_id = le.game_id
                WHERE le.identity_sub = %s
                ORDER BY g.canonical_title
                """,
                (identity_sub,),
            )
            rows = await cur.fetchall()
        return [
            LibraryGameView(game_id=str(row[0]), title=row[1], rawg_enriched=row[2], opencritic_enriched=row[3])
            for row in rows
        ]
