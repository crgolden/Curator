"""Repository for the collections aggregate: ``user_consoles`` reads, the joined per-user candidate-pool
read collection strategies consume, and ``collection_definitions``/``collection_runs``/``collection_items``
persistence.

Same shape as :class:`curator.persistence.repository.Repository`: backed by a shared
:class:`~psycopg_pool.AsyncConnectionPool`, raw parameterized SQL, frozen dataclass results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from psycopg_pool import AsyncConnectionPool

from curator.collections.collection_spec import CollectionSpec
from curator.collections.game_candidate import GameCandidate


@dataclass(frozen=True, slots=True)
class UserConsole:
    """One row from ``user_consoles``."""

    console_id: str
    name: str
    platform: str  # "PS5" | "PS4"
    raw_capacity_gb: float
    update_buffer_gb: float
    routing_genres: tuple[str, ...]
    fill_order: int

    @property
    def effective_capacity_gb(self) -> float:
        """The console's real usable capacity: ``raw_capacity_gb - update_buffer_gb``.

        The one and only place this computation happens -- every consumer (bin-pack, any future
        dashboard) reads this property rather than re-deriving it, so no parallel hardcoded "display"
        capacity number can ever exist.
        """
        return self.raw_capacity_gb - self.update_buffer_gb


@dataclass(frozen=True, slots=True)
class CollectionDefinition:
    """One saved ``collection_definitions`` row -- a reusable, named :class:`CollectionSpec`."""

    definition_id: str
    identity_sub: str
    name: str
    kind: str
    console_id: str | None
    genre_filter: tuple[str, ...]
    min_score: float | None
    aaa_tier_filter: str | None
    sort_order: str | None

    def to_spec(self) -> CollectionSpec:
        """Build the :class:`CollectionSpec` this definition represents, ready for
        :meth:`~curator.collections.collection_orchestrator.CollectionOrchestrator.generate`."""
        return CollectionSpec(
            kind=self.kind,
            console_id=self.console_id,
            genre_filter=self.genre_filter,
            min_score=self.min_score,
            aaa_tier_filter=self.aaa_tier_filter,
            sort_order=self.sort_order,
        )


@dataclass(frozen=True, slots=True)
class RawCandidateRow:
    """One raw joined row from ``library_entries``/``games``/``game_enrichment``, before scoring."""

    game_id: str
    title: str
    genre: str | None
    aaa_tier: str | None
    franchise: str | None
    critical_score: float | None
    oc_score: float | None
    psn_rating: float | None
    is_free_to_play: bool | None
    measured_size_gb: float | None


class CollectionsRepository:
    """DAO over the collections aggregate's tables.

    :param pool: The shared connection pool.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def list_user_consoles(self, identity_sub: str) -> list[UserConsole]:
        """Return a user's consoles, ordered by ``fill_order``."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT console_id, name, platform, raw_capacity_gb, update_buffer_gb, routing_genres, fill_order
                FROM user_consoles WHERE identity_sub = %s ORDER BY fill_order
                """,
                (identity_sub,),
            )
            rows = await cur.fetchall()
        return [
            UserConsole(
                console_id=str(row[0]),
                name=row[1],
                platform=row[2],
                raw_capacity_gb=float(row[3]),
                update_buffer_gb=float(row[4]),
                routing_genres=tuple(row[5] or ()),
                fill_order=row[6],
            )
            for row in rows
        ]

    async def list_candidates(self, identity_sub: str, *, platform: str | None = None) -> list[RawCandidateRow]:
        """Return a user's library, joined with enrichment and the latest measured size (if any).

        :param platform: If given (``"PS5"``/``"PS4"``), only games eligible for that platform
            (``native_ps5`` for PS5, ``ps4_eligible`` for PS4).
        """
        platform_clause = ""
        if platform == "PS5":
            platform_clause = "AND le.native_ps5 = true"
        elif platform == "PS4":
            platform_clause = "AND le.ps4_eligible = true"

        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT g.game_id, g.canonical_title, gen.name, ge.aaa_tier, g.franchise,
                       ge.critical_score, ge.oc_score, ge.psn_rating, ge.is_free_to_play,
                       (
                           SELECT ms.size_gb FROM measured_sizes ms
                           WHERE ms.identity_sub = le.identity_sub AND ms.game_id = g.game_id
                             AND ms.platform = (CASE WHEN le.native_ps5 THEN 'PS5' ELSE 'PS4' END)
                           ORDER BY ms.measured_at DESC LIMIT 1
                       ) AS measured_size_gb
                FROM library_entries le
                JOIN games g ON g.game_id = le.game_id
                LEFT JOIN game_enrichment ge ON ge.game_id = g.game_id
                LEFT JOIN genres gen ON gen.genre_id = ge.genre_id
                WHERE le.identity_sub = %s {platform_clause}
                """,
                (identity_sub,),
            )
            rows = await cur.fetchall()
        return [
            RawCandidateRow(
                game_id=str(row[0]),
                title=row[1],
                genre=row[2],
                aaa_tier=row[3],
                franchise=row[4],
                critical_score=row[5],
                oc_score=row[6],
                psn_rating=row[7],
                is_free_to_play=row[8],
                measured_size_gb=row[9],
            )
            for row in rows
        ]

    async def set_console_install(self, console_id: str, game_id: str, installed: bool) -> None:
        """Set a game's current install state on a specific console.

        The one and only place install-checked-state changes -- never a side effect of a collection run,
        so "physically installed here" and "currently recommended here" stay two distinct facts (checked
        state deliberately never auto-transfers on console reassignment).

        :param console_id: The console.
        :param game_id: The game.
        :param installed: The new install state.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO console_installs (console_id, game_id, installed, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (console_id, game_id) DO UPDATE SET
                    installed = EXCLUDED.installed,
                    updated_at = now()
                """,
                (console_id, game_id, installed),
            )

    async def save_definition(self, identity_sub: str, name: str, spec: CollectionSpec) -> str:
        """Save a named, reusable :class:`~curator.collections.collection_spec.CollectionSpec`.

        :param identity_sub: The Curator user id (Identity's ``sub``) the definition belongs to.
        :param name: A user-chosen name, unique per user (``collection_definitions``'s
            ``UNIQUE (identity_sub, name)`` constraint).
        :param spec: The spec to save.
        :returns: The new definition's id.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO collection_definitions
                    (identity_sub, name, kind, console_id, genre_filter, min_score, aaa_tier_filter, sort_order)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING definition_id
                """,
                (
                    identity_sub,
                    name,
                    spec.kind,
                    spec.console_id,
                    list(spec.genre_filter),
                    spec.min_score,
                    spec.aaa_tier_filter,
                    spec.sort_order,
                ),
            )
            row = await cur.fetchone()
            assert row is not None  # guaranteed by RETURNING definition_id above
        return str(row[0])

    async def list_definitions(self, identity_sub: str) -> list[CollectionDefinition]:
        """Return a user's saved collection definitions, newest first."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT definition_id, identity_sub, name, kind, console_id, genre_filter, min_score,
                       aaa_tier_filter, sort_order
                FROM collection_definitions WHERE identity_sub = %s ORDER BY created_at DESC
                """,
                (identity_sub,),
            )
            rows = await cur.fetchall()
        return [self._to_definition(row) for row in rows]

    async def get_definition(self, identity_sub: str, definition_id: str) -> CollectionDefinition | None:
        """Return one of a user's saved definitions, or ``None`` if it doesn't exist or isn't theirs.

        Scoped to ``identity_sub`` in the query itself (not filtered after the fact), so a definition id
        belonging to another user is indistinguishable from an unknown one -- no cross-user leakage.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT definition_id, identity_sub, name, kind, console_id, genre_filter, min_score,
                       aaa_tier_filter, sort_order
                FROM collection_definitions WHERE identity_sub = %s AND definition_id = %s
                """,
                (identity_sub, definition_id),
            )
            row = await cur.fetchone()
        return self._to_definition(row) if row is not None else None

    @staticmethod
    def _to_definition(row: Any) -> CollectionDefinition:
        return CollectionDefinition(
            definition_id=str(row[0]),
            identity_sub=str(row[1]),
            name=row[2],
            kind=row[3],
            console_id=str(row[4]) if row[4] is not None else None,
            genre_filter=tuple(row[5] or ()),
            min_score=float(row[6]) if row[6] is not None else None,
            aaa_tier_filter=row[7],
            sort_order=row[8],
        )

    async def save_run(
        self,
        identity_sub: str,
        definition_id: str | None,
        spec_snapshot: dict[str, Any],
        included: list[GameCandidate],
        excluded: list[GameCandidate],
    ) -> str:
        """Persist one collection-generation run and its per-game outcomes.

        :param identity_sub: The Curator user id (Identity's ``sub``).
        :param definition_id: The saved definition this run used, or ``None`` for an inline/preview spec.
        :param spec_snapshot: The spec actually used, so the run stays explainable after the fact.
        :param included: The games the run included, in rank order.
        :param excluded: The games the run considered but did not include.
        :returns: The new run's id.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO collection_runs (identity_sub, definition_id, spec_snapshot)
                VALUES (%s, %s, %s)
                RETURNING run_id
                """,
                (identity_sub, definition_id, json.dumps(spec_snapshot)),
            )
            row = await cur.fetchone()
            assert row is not None  # guaranteed by RETURNING run_id above
            run_id = str(row[0])

            for rank, candidate in enumerate(included, start=1):
                await cur.execute(
                    """
                    INSERT INTO collection_items (run_id, game_id, included, rank, composite_score, rank_score, size_gb)
                    VALUES (%s, %s, true, %s, %s, %s, %s)
                    """,
                    (
                        run_id,
                        candidate.game_id,
                        rank,
                        candidate.composite_score,
                        candidate.rank_score,
                        candidate.size_gb,
                    ),
                )
            for candidate in excluded:
                await cur.execute(
                    """
                    INSERT INTO collection_items (run_id, game_id, included, composite_score, rank_score, size_gb)
                    VALUES (%s, %s, false, %s, %s, %s)
                    """,
                    (run_id, candidate.game_id, candidate.composite_score, candidate.rank_score, candidate.size_gb),
                )
        return run_id
