"""Repository over the PS Plus catalog tables (``0058_ps_plus_catalog.sql``): the walked categories, each
walk, and the per-title memberships with their lifecycle, plus the read side of ``GET /me/ps-plus-rotation``.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from psycopg import AsyncCursor
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from curator.psn._media import cover_image_url
from curator.psn.store_client import StoreProduct

PS_PLUS_WALK_ADVISORY_LOCK_CLASS = 4
"""Mirrored as ``CuratorAdvisoryLocks.PsPlusWalk`` in ``Functions`` so the classid space stays documented in
both runtimes; only this runtime takes the lock."""

PS_PLUS_REWARD_MEMBERSHIP_TYPE = "PS_PLUS"

WALK_STOPPED_REASONS = (
    "query_rotated",
    "filter_not_applied",
    "no_products",
    "page_budget_exhausted",
    "category_renamed",
)

_MEMBERSHIP_COLUMNS = """
       m.title_id, c.tier, m.store_product_id, m.raw, {since_column} AS since_at,
       COALESCE(cache.game_id, owned.game_id) AS game_id, g.canonical_title
"""

_MEMBERSHIP_FROM = """
FROM ps_plus_catalog_memberships m
JOIN ps_plus_catalog_categories c ON c.category_id = m.category_id
LEFT JOIN LATERAL (
    SELECT pcc.game_id FROM psn_catalog_cache pcc
    WHERE pcc.title_id = m.title_id AND pcc.game_id IS NOT NULL
    ORDER BY pcc.fetched_at DESC LIMIT 1
) cache ON true
LEFT JOIN LATERAL (
    SELECT le.game_id FROM library_entries le
    WHERE le.title_id = m.title_id
    ORDER BY le.last_seen_at DESC LIMIT 1
) owned ON true
LEFT JOIN games g ON g.game_id = COALESCE(cache.game_id, owned.game_id)
"""

UNCLAIMED_SQL = (
    "SELECT"
    + _MEMBERSHIP_COLUMNS.format(since_column="m.first_seen_at")
    + _MEMBERSHIP_FROM
    + """
WHERE m.left_at IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM library_entries held
      WHERE held.identity_sub = %s AND held.title_id = m.title_id AND held.is_active
  )
ORDER BY m.first_seen_at DESC, m.title_id
"""
)
"""Catalog members the caller holds no active entry for. Parameters: ``(identity_sub,)``."""

ADDED_SQL = (
    "SELECT"
    + _MEMBERSHIP_COLUMNS.format(since_column="m.first_seen_at")
    + _MEMBERSHIP_FROM
    + """
WHERE m.left_at IS NULL AND m.first_seen_at > %s
ORDER BY m.first_seen_at DESC, m.title_id
"""
)
"""Members first seen after an instant. Parameters: ``(since,)``."""

LEAVING_SQL = (
    "SELECT"
    + _MEMBERSHIP_COLUMNS.format(since_column="m.left_at")
    + _MEMBERSHIP_FROM
    + """
WHERE m.left_at IS NOT NULL AND m.left_at > %s
  AND EXISTS (
      SELECT 1 FROM entitlement_snapshots es
      WHERE es.identity_sub = %s AND es.title_id = m.title_id AND es.reward_membership_type = %s
  )
ORDER BY m.left_at DESC, m.title_id
"""
)
"""Members that left after an instant and that the caller obtained through PS Plus. Parameters:
``(since, identity_sub, PS_PLUS_REWARD_MEMBERSHIP_TYPE)``."""

LAPSED_SQL = """
SELECT DISTINCT ON (es.title_id)
       es.title_id, c.tier, m.store_product_id, m.raw, es.last_seen_at AS since_at,
       le.game_id, g.canonical_title
FROM entitlement_snapshots es
LEFT JOIN library_entries le ON le.identity_sub = es.identity_sub AND le.title_id = es.title_id
LEFT JOIN games g ON g.game_id = le.game_id
LEFT JOIN ps_plus_catalog_memberships m ON m.title_id = es.title_id
LEFT JOIN ps_plus_catalog_categories c ON c.category_id = m.category_id
WHERE es.identity_sub = %s
  AND es.reward_membership_type = %s
  AND es.active = false
  AND es.title_id IS NOT NULL
ORDER BY es.title_id, es.last_seen_at DESC
"""
"""The caller's PS-Plus-sourced entitlements that PSN now reports inactive. Parameters:
``(identity_sub, PS_PLUS_REWARD_MEMBERSHIP_TYPE)``."""

CATEGORY_WALK_STATE_SQL = """
SELECT c.category_id, c.tier, latest.completed_at, latest.distinct_products, previous.completed_at
FROM ps_plus_catalog_categories c
LEFT JOIN LATERAL (
    SELECT w.completed_at, w.distinct_products FROM ps_plus_catalog_walks w
    WHERE w.category_id = c.category_id AND w.completed_at IS NOT NULL AND w.stopped_reason IS NULL
    ORDER BY w.completed_at DESC LIMIT 1
) latest ON true
LEFT JOIN LATERAL (
    SELECT w.completed_at FROM ps_plus_catalog_walks w
    WHERE w.category_id = c.category_id AND w.completed_at IS NOT NULL AND w.stopped_reason IS NULL
    ORDER BY w.completed_at DESC OFFSET 1 LIMIT 1
) previous ON true
ORDER BY c.tier
"""
"""Each category with its latest and previous completed walk. No parameters."""


@dataclass(frozen=True, slots=True)
class PsPlusCategory:
    """One walked storefront category."""

    category_id: str
    tier: str
    locale: str
    reporting_name_prefix: str


@dataclass(frozen=True, slots=True)
class PsPlusCategoryState:
    """A category's most recent completed walk, and the one before it.

    :param previous_completed_at: ``None`` until the category has completed two walks.
    """

    category_id: str
    tier: str
    walked_at: datetime | None
    total: int
    previous_completed_at: datetime | None


@dataclass(frozen=True, slots=True)
class PsPlusTitle:
    """One title in a rotation report.

    :param game_id: The catalog game the title resolves to, or ``None`` when neither the store cache nor any
        library holds it.
    :param tier: ``extra``/``premium`` for a catalog member; ``None`` for a lapsed entitlement no walked
        category lists.
    :param since_at: When the title entered the list it is reported in.
    """

    title_id: str
    game_id: str | None
    title: str | None
    tier: str | None
    platforms: tuple[str, ...]
    cover_image_url: str | None
    store_product_id: str | None
    since_at: datetime | None


@dataclass(frozen=True, slots=True)
class PsPlusRotationReport:
    """The ``GET /me/ps-plus-rotation`` body.

    :param since: The instant the ``added``/``leaving`` diff is measured from; ``None`` until every walked
        category has completed twice, in which case both lists are empty.
    """

    catalog_walked_at: datetime | None
    since: datetime | None
    added: list[PsPlusTitle]
    leaving: list[PsPlusTitle]
    unclaimed: list[PsPlusTitle]
    lapsed: list[PsPlusTitle]
    categories: list[PsPlusCategoryState]


@dataclass(frozen=True, slots=True)
class PsPlusRotationSummary:
    """The ``GET /me/ps-plus-rotation/summary`` body: the two counts with an action attached."""

    catalog_walked_at: datetime | None
    unclaimed: int
    leaving: int


class PsPlusWalkWriter:
    """The write surface of one category walk, bound to the cursor that holds the walk's transaction and
    advisory lock. Obtained from :meth:`PsPlusRepository.walk`.
    """

    def __init__(self, cursor: AsyncCursor[Any], category_id: str) -> None:
        self._cursor = cursor
        self._category_id = category_id

    async def begin(self, started_at: datetime) -> str:
        """Insert the walk row and return its id."""
        walk_id = str(uuid.uuid4())
        await self._cursor.execute(
            "INSERT INTO ps_plus_catalog_walks (walk_id, category_id, started_at) VALUES (%s, %s, %s)",
            (walk_id, self._category_id, started_at),
        )
        return walk_id

    async def record_products(self, walk_id: str, products: Sequence[StoreProduct], seen_at: datetime) -> int:
        """Upsert one page's products as memberships of this category.

        A product without an ``npTitleId`` is skipped: the membership key is the title id. A returning
        title keeps ``first_seen_at`` and has ``left_at`` cleared.

        :returns: How many products were written.
        """
        written = 0
        for product in products:
            if not product.np_title_id:
                continue
            await self._cursor.execute(
                """
                INSERT INTO ps_plus_catalog_memberships (
                    title_id, category_id, store_product_id, classification,
                    first_seen_walk_id, last_seen_walk_id, first_seen_at, last_seen_at, left_at, raw
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, %s)
                ON CONFLICT (title_id, category_id) DO UPDATE SET
                    store_product_id = EXCLUDED.store_product_id,
                    classification = EXCLUDED.classification,
                    last_seen_walk_id = EXCLUDED.last_seen_walk_id,
                    last_seen_at = EXCLUDED.last_seen_at,
                    left_at = NULL,
                    raw = EXCLUDED.raw
                """,
                (
                    product.np_title_id,
                    self._category_id,
                    product.product_id,
                    product.classification,
                    walk_id,
                    walk_id,
                    seen_at,
                    seen_at,
                    Jsonb(dict(product.raw)),
                ),
            )
            written += 1
        return written

    async def complete(self, walk_id: str, completed_at: datetime, reported_total: int, distinct_products: int) -> None:
        """Mark the walk completed with what the gateway reported and what the walk actually saw."""
        await self._cursor.execute(
            """
            UPDATE ps_plus_catalog_walks
            SET completed_at = %s, reported_total = %s, distinct_products = %s
            WHERE walk_id = %s
            """,
            (completed_at, reported_total, distinct_products, walk_id),
        )

    async def mark_departures(self, walk_id: str, completed_at: datetime) -> int:
        """Set ``left_at`` on every membership of this category the completed walk did not see.

        :returns: How many memberships were marked.
        """
        await self._cursor.execute(
            """
            UPDATE ps_plus_catalog_memberships
            SET left_at = %s
            WHERE category_id = %s AND last_seen_walk_id <> %s AND left_at IS NULL
            """,
            (completed_at, self._category_id, walk_id),
        )
        return self._cursor.rowcount

    async def stop(self, walk_id: str, reason: str, reported_total: int | None, distinct_products: int) -> None:
        """Record why the walk stopped short. ``completed_at`` stays NULL, so no departure is ever derived."""
        await self._cursor.execute(
            """
            UPDATE ps_plus_catalog_walks
            SET stopped_reason = %s, reported_total = %s, distinct_products = %s
            WHERE walk_id = %s
            """,
            (reason, reported_total, distinct_products, walk_id),
        )


class PsPlusRepository:
    """DAO over the PS Plus catalog tables.

    :param pool: The shared connection pool.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def list_categories(self) -> list[PsPlusCategory]:
        """Return every category the walk covers, in tier order."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT category_id, tier, locale, reporting_name_prefix FROM ps_plus_catalog_categories ORDER BY tier"
            )
            rows = await cur.fetchall()
        return [PsPlusCategory(str(row[0]), row[1], row[2], row[3]) for row in rows]

    @asynccontextmanager
    async def walk(self, category_id: str) -> AsyncIterator[PsPlusWalkWriter]:
        """Open one category walk: a transaction holding the per-category advisory lock until it commits.

        Two workers walking one category serialize on the lock rather than interleaving pages; everything
        the writer records lands together at exit, and an exception inside the block rolls it all back.
        """
        async with self._pool.connection() as conn, conn.transaction(), conn.cursor() as cur:
            await cur.execute(
                "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                (PS_PLUS_WALK_ADVISORY_LOCK_CLASS, f"ps-plus-walk:{category_id}"),
            )
            yield PsPlusWalkWriter(cur, category_id)

    async def latest_walk_started_at(self) -> datetime | None:
        """Return when the most recent walk of any category started, or ``None`` when none has."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT max(started_at) FROM ps_plus_catalog_walks")
            row = await cur.fetchone()
        return row[0] if row is not None else None

    async def category_states(self) -> list[PsPlusCategoryState]:
        """Return each category's latest and previous completed walk."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(CATEGORY_WALK_STATE_SQL)
            rows = await cur.fetchall()
        return [
            PsPlusCategoryState(
                category_id=str(row[0]),
                tier=row[1],
                walked_at=row[2],
                total=int(row[3] or 0),
                previous_completed_at=row[4],
            )
            for row in rows
        ]

    async def rotation_report(self, identity_sub: str) -> PsPlusRotationReport:
        """Build the caller's rotation report from the walked catalog and their own entitlements.

        :param identity_sub: The caller's Identity ``sub``.
        """
        categories = await self.category_states()
        catalog_walked_at = _latest(state.walked_at for state in categories)
        since = _earliest(state.previous_completed_at for state in categories)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(UNCLAIMED_SQL, (identity_sub,))
            unclaimed = [_to_title(row) for row in await cur.fetchall()]
            added: list[PsPlusTitle] = []
            leaving: list[PsPlusTitle] = []
            if since is not None:
                await cur.execute(ADDED_SQL, (since,))
                added = [_to_title(row) for row in await cur.fetchall()]
                await cur.execute(LEAVING_SQL, (since, identity_sub, PS_PLUS_REWARD_MEMBERSHIP_TYPE))
                leaving = [_to_title(row) for row in await cur.fetchall()]
            await cur.execute(LAPSED_SQL, (identity_sub, PS_PLUS_REWARD_MEMBERSHIP_TYPE))
            lapsed = [_to_title(row) for row in await cur.fetchall()]
        return PsPlusRotationReport(
            catalog_walked_at=catalog_walked_at,
            since=since,
            added=added,
            leaving=leaving,
            unclaimed=unclaimed,
            lapsed=lapsed,
            categories=categories,
        )

    async def rotation_summary(self, identity_sub: str) -> PsPlusRotationSummary:
        """Count the caller's unclaimed and leaving titles without materializing the report."""
        report = await self.rotation_report(identity_sub)
        return PsPlusRotationSummary(
            catalog_walked_at=report.catalog_walked_at, unclaimed=len(report.unclaimed), leaving=len(report.leaving)
        )


def _latest(values: Any) -> datetime | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _earliest(values: Any) -> datetime | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None


def _to_title(row: Sequence[Any]) -> PsPlusTitle:
    raw = row[3] if isinstance(row[3], dict) else {}
    name = raw.get("name")
    platforms = raw.get("platforms")
    return PsPlusTitle(
        title_id=str(row[0]),
        game_id=str(row[5]) if row[5] is not None else None,
        title=row[6] if row[6] is not None else (name if isinstance(name, str) and name.strip() else None),
        tier=row[1],
        platforms=tuple(str(platform) for platform in platforms) if isinstance(platforms, list) else (),
        cover_image_url=cover_image_url(raw.get("media")),
        store_product_id=row[2],
        since_at=row[4],
    )
