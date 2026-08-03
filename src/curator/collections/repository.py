"""Repository for the collections aggregate: ``user_consoles`` reads, the joined per-user candidate-pool
read collection strategies consume, ``collection_definitions``/``collection_definition_items`` (a
collection and its stored membership), and ``collection_runs``/``collection_items`` (run history).

The two persistence pairs are deliberately separate. ``collection_definition_items`` is what a collection
*is* -- an explicit, ordered, materialized list the owner curated. ``collection_items`` is what the
algorithm *proposed* on a given date, rejections included. Merging them would make a suggestion
indistinguishable from a decision.

Same shape as :class:`curator.persistence.repository.Repository`: backed by a shared
:class:`~psycopg_pool.AsyncConnectionPool`, raw parameterized SQL, frozen dataclass results.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any

from psycopg_pool import AsyncConnectionPool

from curator.collections.collection_spec import CollectionSpec
from curator.collections.filter_predicate import FilterPredicate, parse_predicate, predicate_to_dict
from curator.collections.game_candidate import GameCandidate


def _deduplicate(game_ids: tuple[str, ...]) -> list[str]:
    """Drop repeated ids, keeping first occurrence -- a repeat would collide on the items primary key."""
    seen: set[str] = set()
    unique: list[str] = []
    for game_id in game_ids:
        if game_id not in seen:
            seen.add(game_id)
            unique.append(game_id)
    return unique


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
    #: Purely informational (0018) -- drives consoles_routes.py's default-capacity lookup at creation
    #: time only. raw_capacity_gb is always independently editable regardless of what's recorded here.
    model: str | None = None

    @property
    def effective_capacity_gb(self) -> float:
        """The console's real usable capacity: ``raw_capacity_gb - update_buffer_gb``.

        The one and only place this computation happens -- every consumer (bin-pack, any future
        dashboard) reads this property rather than re-deriving it, so no parallel hardcoded "display"
        capacity number can ever exist.
        """
        return self.raw_capacity_gb - self.update_buffer_gb


@dataclass(frozen=True, slots=True)
class StorageDevice:
    """One row from ``storage_devices`` -- a swappable M.2/USB drive, distinct from a console's own
    built-in capacity (:class:`UserConsole`)."""

    device_id: str
    identity_sub: str
    console_id: str | None
    name: str
    kind: str  # "m2" | "usb"
    capacity_gb: float
    buffer_gb: float

    @property
    def effective_capacity_gb(self) -> float:
        """The device's real usable capacity: ``capacity_gb - buffer_gb``. Mirrors
        :attr:`UserConsole.effective_capacity_gb` -- same computation, same "one place only" rationale."""
        return self.capacity_gb - self.buffer_gb


@dataclass(frozen=True, slots=True)
class CollectionDefinition:
    """One saved ``collection_definitions`` row -- a named collection.

    The filter fields (``genre_filter``/``min_score``/``aaa_tier_filter``) are **provenance**, not
    membership: they record how the collection was originally assembled and drive
    :meth:`to_spec` when the owner asks for a fresh proposal, but the collection's actual contents live
    in ``collection_definition_items`` (see :class:`CollectionItem`) and are never re-derived from these.
    """

    definition_id: str
    identity_sub: str
    name: str
    kind: str
    console_id: str | None
    genre_filter: tuple[str, ...]
    min_score: float | None
    aaa_tier_filter: str | None
    sort_order: str | None
    description: str | None = None
    include_inactive: bool = False
    min_percent_completed: int | None = None
    #: An OR-capable predicate tree (WP8), replacing the flat genre/score/tier fields above when set. See
    #: :mod:`curator.collections.filter_predicate`'s module docstring for why it's a separate field rather
    #: than folded into them.
    filter_predicate: FilterPredicate | None = None
    #: ``"private"``/``"unlisted"``/``"public"`` (0019) -- a per-collection gate underneath the existing
    #: account-wide ``user_profiles.show_collections``/``is_public`` gate, not a replacement for it.
    visibility: str = "private"
    #: Generated client-side at creation for every collection regardless of visibility (0019) -- inert
    #: unless ``visibility != "private"``. See migration 0019's header for why it's never lazy.
    share_slug: str | None = None
    #: How many games this collection currently holds. Cheap (one JOIN/subquery), so it's included on
    #: every read rather than requiring a second ``list_definition_items`` call just to answer "how big
    #: is this" -- what ``ProfileDefinitionResponse`` was missing before 0019.
    item_count: int = 0

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
            include_inactive=self.include_inactive,
            min_percent_completed=self.min_percent_completed,
            filter_predicate=self.filter_predicate,
        )


@dataclass(frozen=True, slots=True)
class CollectionItem:
    """One stored member of a collection, joined with the shared catalog for display.

    Everything here comes from cross-user tables (``games``/``game_enrichment``/``genres``/
    ``psn_catalog_cache``, plus ``entitlement_snapshots`` joined by concept id alone for fallback
    artwork), so the same row renders identically for the owner, another signed-in viewer, or -- once a
    public share route exists -- an anonymous one.
    """

    game_id: str
    rank: int
    title: str
    franchise: str | None
    genre: str | None
    aaa_tier: str | None
    critical_score: float | None
    oc_score: float | None
    psn_rating: float | None
    cover_image_url: str | None
    #: Whether the collection's **owner** can still play this game. Deliberately the owner's access and
    #: not the viewer's: a shared collection describes what its author curated, so it must read the same
    #: to everyone. ``False`` marks a title the owner has since lost access to -- it stays in the list
    #: (it is their list) and is rendered as unavailable rather than silently disappearing.
    owner_has_access: bool


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
    #: This entry's matched PSN trophy-title id, or ``None`` if never matched -- see
    #: ``0014_library_entries_trophy_match.sql``. ``curator.psn.trophy_completion`` uses this for a cheap,
    #: exact completion-percentage lookup instead of fuzzy name matching.
    np_communication_id: str | None = None
    #: Stored trophy completion percentage (``0015_library_entries_trophy_progress.sql``). Read straight
    #: from the row, so scoring a candidate pool needs no PSN call and no name matching.
    percent_completed: int | None = None


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
                SELECT console_id, name, platform, raw_capacity_gb, update_buffer_gb, routing_genres,
                       fill_order, model
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
                model=row[7],
            )
            for row in rows
        ]

    async def create_console(
        self,
        identity_sub: str,
        *,
        name: str,
        platform: str,
        raw_capacity_gb: float,
        update_buffer_gb: float = 0.0,
        routing_genres: tuple[str, ...] = (),
        fill_order: int = 0,
        model: str | None = None,
    ) -> UserConsole:
        """Create a console. ``platform`` is fixed at creation -- a console's platform never changes, so
        it is not one of :meth:`update_console`'s editable fields. ``model`` is purely informational
        (0018) -- see :class:`UserConsole`."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO user_consoles
                    (identity_sub, name, platform, raw_capacity_gb, update_buffer_gb, routing_genres, fill_order, model)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING console_id
                """,
                (
                    identity_sub,
                    name,
                    platform,
                    raw_capacity_gb,
                    update_buffer_gb,
                    list(routing_genres),
                    fill_order,
                    model,
                ),
            )
            row = await cur.fetchone()
        assert row is not None
        return UserConsole(
            console_id=str(row[0]),
            name=name,
            platform=platform,
            raw_capacity_gb=raw_capacity_gb,
            update_buffer_gb=update_buffer_gb,
            routing_genres=routing_genres,
            fill_order=fill_order,
            model=model,
        )

    async def get_console(self, identity_sub: str, console_id: str) -> UserConsole | None:
        """Return one console, scoped to its owner -- ``None`` if it doesn't exist or belongs to someone
        else, so a route can 404 either case identically rather than leaking which one it was."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT console_id, name, platform, raw_capacity_gb, update_buffer_gb, routing_genres,
                       fill_order, model
                FROM user_consoles WHERE identity_sub = %s AND console_id = %s
                """,
                (identity_sub, console_id),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return UserConsole(
            console_id=str(row[0]),
            name=row[1],
            platform=row[2],
            raw_capacity_gb=float(row[3]),
            update_buffer_gb=float(row[4]),
            routing_genres=tuple(row[5] or ()),
            fill_order=row[6],
            model=row[7],
        )

    async def update_console(
        self,
        identity_sub: str,
        console_id: str,
        *,
        name: str | None = None,
        raw_capacity_gb: float | None = None,
        update_buffer_gb: float | None = None,
        routing_genres: tuple[str, ...] | None = None,
        fill_order: int | None = None,
    ) -> UserConsole | None:
        """Patch a console's editable fields (``None`` leaves a field unchanged). ``platform`` is not
        editable -- see :meth:`create_console`.

        :returns: The updated console, or ``None`` if it doesn't exist / belong to this user.
        """
        existing = await self.get_console(identity_sub, console_id)
        if existing is None:
            return None
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE user_consoles
                SET name = %s, raw_capacity_gb = %s, update_buffer_gb = %s, routing_genres = %s,
                    fill_order = %s, updated_at = now()
                WHERE identity_sub = %s AND console_id = %s
                """,
                (
                    existing.name if name is None else name,
                    existing.raw_capacity_gb if raw_capacity_gb is None else raw_capacity_gb,
                    existing.update_buffer_gb if update_buffer_gb is None else update_buffer_gb,
                    list(existing.routing_genres if routing_genres is None else routing_genres),
                    existing.fill_order if fill_order is None else fill_order,
                    identity_sub,
                    console_id,
                ),
            )
        return await self.get_console(identity_sub, console_id)

    async def delete_console(self, identity_sub: str, console_id: str) -> bool:
        """Delete a console. Cascades to its ``console_installs`` rows (``0009``); any attached
        :class:`StorageDevice` is detached, not deleted -- a swappable drive survives its console going
        away, matching how a physical drive isn't destroyed by unplugging it.

        :returns: Whether a row was actually deleted (``False`` if it didn't exist / belong to this user).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM user_consoles WHERE identity_sub = %s AND console_id = %s",
                (identity_sub, console_id),
            )
            return cur.rowcount > 0

    async def create_storage_device(
        self,
        identity_sub: str,
        *,
        name: str,
        kind: str,
        capacity_gb: float,
        buffer_gb: float = 0.0,
        console_id: str | None = None,
    ) -> StorageDevice:
        """Create a storage device, optionally already attached to a console. ``kind`` is fixed at
        creation, matching :meth:`create_console`'s ``platform``."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO storage_devices (identity_sub, console_id, name, kind, capacity_gb, buffer_gb)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING device_id
                """,
                (identity_sub, console_id, name, kind, capacity_gb, buffer_gb),
            )
            row = await cur.fetchone()
        assert row is not None
        return StorageDevice(
            device_id=str(row[0]),
            identity_sub=identity_sub,
            console_id=console_id,
            name=name,
            kind=kind,
            capacity_gb=capacity_gb,
            buffer_gb=buffer_gb,
        )

    async def list_storage_devices(self, identity_sub: str) -> list[StorageDevice]:
        """Return every storage device a user owns, attached or not."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT device_id, console_id, name, kind, capacity_gb, buffer_gb
                FROM storage_devices WHERE identity_sub = %s ORDER BY name
                """,
                (identity_sub,),
            )
            rows = await cur.fetchall()
        return [
            StorageDevice(
                device_id=str(row[0]),
                identity_sub=identity_sub,
                console_id=str(row[1]) if row[1] is not None else None,
                name=row[2],
                kind=row[3],
                capacity_gb=float(row[4]),
                buffer_gb=float(row[5]),
            )
            for row in rows
        ]

    async def get_storage_device(self, identity_sub: str, device_id: str) -> StorageDevice | None:
        """Return one storage device, scoped to its owner -- ``None`` if it doesn't exist or belongs to
        someone else."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT device_id, console_id, name, kind, capacity_gb, buffer_gb
                FROM storage_devices WHERE identity_sub = %s AND device_id = %s
                """,
                (identity_sub, device_id),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        return StorageDevice(
            device_id=str(row[0]),
            identity_sub=identity_sub,
            console_id=str(row[1]) if row[1] is not None else None,
            name=row[2],
            kind=row[3],
            capacity_gb=float(row[4]),
            buffer_gb=float(row[5]),
        )

    async def update_storage_device(
        self,
        identity_sub: str,
        device_id: str,
        *,
        name: str | None = None,
        capacity_gb: float | None = None,
        buffer_gb: float | None = None,
    ) -> StorageDevice | None:
        """Patch a device's editable fields (``None`` leaves a field unchanged). Does not touch
        ``console_id`` -- see :meth:`attach_storage_device`/:meth:`detach_storage_device`, which are the
        only place attachment changes, so it's never an accidental side effect of an unrelated rename.

        :returns: The updated device, or ``None`` if it doesn't exist / belong to this user.
        """
        existing = await self.get_storage_device(identity_sub, device_id)
        if existing is None:
            return None
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE storage_devices SET name = %s, capacity_gb = %s, buffer_gb = %s, updated_at = now()
                WHERE identity_sub = %s AND device_id = %s
                """,
                (
                    existing.name if name is None else name,
                    existing.capacity_gb if capacity_gb is None else capacity_gb,
                    existing.buffer_gb if buffer_gb is None else buffer_gb,
                    identity_sub,
                    device_id,
                ),
            )
        return await self.get_storage_device(identity_sub, device_id)

    async def delete_storage_device(self, identity_sub: str, device_id: str) -> bool:
        """Delete a storage device (cascades to its ``storage_device_installs`` rows).

        :returns: Whether a row was actually deleted (``False`` if it didn't exist / belong to this user).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM storage_devices WHERE identity_sub = %s AND device_id = %s",
                (identity_sub, device_id),
            )
            return cur.rowcount > 0

    async def set_storage_device_attachment(
        self, identity_sub: str, device_id: str, console_id: str | None
    ) -> StorageDevice | None:
        """Attach a device to ``console_id``, or detach it if ``console_id`` is ``None``. The one and only
        place a device's ``console_id`` changes -- callers must validate ``console_id`` belongs to this
        user themselves (this method only scopes the device, matching how ``collections_routes`` already
        validates a collection's ``console_id`` at its own call site rather than pushing that check into
        every repository method that touches a console_id).

        :returns: The updated device, or ``None`` if it doesn't exist / belong to this user.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE storage_devices SET console_id = %s, updated_at = now()
                WHERE identity_sub = %s AND device_id = %s
                """,
                (console_id, identity_sub, device_id),
            )
        return await self.get_storage_device(identity_sub, device_id)

    async def set_storage_device_install(self, device_id: str, game_id: str, installed: bool) -> None:
        """Set a game's install state on a specific storage device. Mirrors :meth:`set_console_install`
        exactly, including the same never-a-side-effect-of-a-collection-run rule."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO storage_device_installs (device_id, game_id, installed, updated_at)
                VALUES (%s, %s, %s, now())
                ON CONFLICT (device_id, game_id) DO UPDATE SET
                    installed = EXCLUDED.installed,
                    updated_at = now()
                """,
                (device_id, game_id, installed),
            )

    async def list_installed_game_ids(self, console_id: str) -> set[str]:
        """Return the game ids currently marked installed on a console's own built-in storage (not any
        attached device's installs -- see :meth:`list_storage_device_installed_game_ids`)."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT game_id FROM console_installs WHERE console_id = %s AND installed = true", (console_id,)
            )
            rows = await cur.fetchall()
        return {str(row[0]) for row in rows}

    async def list_storage_device_installed_game_ids(self, device_id: str) -> set[str]:
        """Return the game ids currently marked installed on one storage device."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT game_id FROM storage_device_installs WHERE device_id = %s AND installed = true",
                (device_id,),
            )
            rows = await cur.fetchall()
        return {str(row[0]) for row in rows}

    async def list_candidates(
        self,
        identity_sub: str,
        *,
        platform: str | None = None,
        include_inactive: bool = False,
        min_percent_completed: int | None = None,
    ) -> list[RawCandidateRow]:
        """Return a user's library, joined with enrichment and the latest measured size (if any).

        Games the user has explicitly excluded (``library_exclusions``), games they can no longer play
        (``is_active = false``), and -- when asked -- games below a trophy-completion floor are filtered
        out here, so every collection strategy inherits all three without having to re-apply them. This is
        the single chokepoint every candidate pool flows through.

        :param platform: If given (``"PS5"``/``"PS4"``), only games eligible for that platform
            (``native_ps5`` for PS5, ``ps4_eligible`` for PS4).
        :param include_inactive: Draw on lapsed entitlements too. Off by default, so a collection is
            built from what its owner can actually launch unless they ask otherwise.
        :param min_percent_completed: Minimum stored trophy completion to include, or ``None`` for no
            floor. Expressible as SQL only because ``0015_library_entries_trophy_progress.sql`` persists
            the percentage: while it lived exclusively in Redis this had to be applied in Python *after*
            fetching every game's trophy data, which is what made a narrow collection spec cost a
            full-library PSN resolution.
        """
        platform_clause = ""
        if platform == "PS5":
            platform_clause = "AND le.native_ps5 = true"
        elif platform == "PS4":
            platform_clause = "AND le.ps4_eligible = true"

        active_clause = "" if include_inactive else "AND le.is_active = true"

        params: list[Any] = [identity_sub]
        completion_clause = ""
        if min_percent_completed is not None:
            # A game with no stored percentage can't satisfy a floor, so this excludes NULLs -- but only
            # when the user has *some* progress stored. A user who has never had a trophy refresh (or who
            # has harvest_trophies off) has NULL everywhere, and applying the floor would empty their
            # collection for a reason that has nothing to do with the collection. That is the same guard
            # curator.collections.filter_list_strategy's completion_available parameter provides for the
            # in-memory path, expressed here so it survives the move into SQL.
            completion_clause = """
                AND (
                    le.trophy_percent_completed >= %s
                    OR NOT EXISTS (
                        SELECT 1 FROM library_entries probe
                        WHERE probe.identity_sub = le.identity_sub
                          AND probe.trophy_percent_completed IS NOT NULL
                    )
                )
            """
            params.append(min_percent_completed)

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
                       ) AS measured_size_gb,
                       le.np_communication_id, le.trophy_percent_completed
                FROM library_entries le
                JOIN games g ON g.game_id = le.game_id
                LEFT JOIN game_enrichment ge ON ge.game_id = g.game_id
                LEFT JOIN genres gen ON gen.genre_id = ge.genre_id
                WHERE le.identity_sub = %s {platform_clause} {active_clause} {completion_clause}
                  AND NOT EXISTS (
                      SELECT 1 FROM library_exclusions lx
                      WHERE lx.identity_sub = le.identity_sub AND lx.game_id = g.game_id
                  )
                """,
                tuple(params),
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
                np_communication_id=row[10],
                percent_completed=row[11],
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

    async def save_definition(
        self,
        identity_sub: str,
        name: str,
        spec: CollectionSpec,
        *,
        description: str | None = None,
        game_ids: tuple[str, ...] = (),
    ) -> str:
        """Save a named collection: its metadata, its authoring spec, and its membership.

        The definition row and its items are written in one connection block, so a failure partway
        through the item inserts leaves no half-populated collection behind.

        :param identity_sub: The Curator user id (Identity's ``sub``) the definition belongs to.
        :param name: A user-chosen name, unique per user (``collection_definitions``'s
            ``UNIQUE (identity_sub, name)`` constraint).
        :param spec: The spec that produced this collection, kept as provenance.
        :param description: Optional free text describing the collection.
        :param game_ids: The collection's members, in the caller's chosen order. Duplicates are dropped
            (first occurrence wins) rather than colliding on ``collection_definition_items``' primary key.
        :returns: The new definition's id.
        """
        # Generated for every collection regardless of visibility -- see migration 0019's header for why
        # this is unconditional rather than lazy (only assigned when sharing is first turned on).
        share_slug = secrets.token_urlsafe(9)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO collection_definitions
                    (identity_sub, name, description, kind, console_id, genre_filter, min_score,
                     aaa_tier_filter, sort_order, include_inactive, min_percent_completed, filter_predicate,
                     share_slug)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING definition_id
                """,
                (
                    identity_sub,
                    name,
                    description,
                    spec.kind,
                    spec.console_id,
                    list(spec.genre_filter),
                    spec.min_score,
                    spec.aaa_tier_filter,
                    spec.sort_order,
                    spec.include_inactive,
                    spec.min_percent_completed,
                    json.dumps(predicate_to_dict(spec.filter_predicate)) if spec.filter_predicate is not None else None,
                    share_slug,
                ),
            )
            row = await cur.fetchone()
            assert row is not None  # guaranteed by RETURNING definition_id above
            definition_id = str(row[0])
            await self._insert_items(cur, definition_id, game_ids)
        return definition_id

    @staticmethod
    async def _insert_items(cur: Any, definition_id: str, game_ids: tuple[str, ...]) -> None:
        """Write a collection's membership rows, ranked by position in ``game_ids``."""
        for rank, game_id in enumerate(_deduplicate(game_ids), start=1):
            await cur.execute(
                """
                INSERT INTO collection_definition_items (definition_id, game_id, rank)
                VALUES (%s, %s, %s)
                """,
                (definition_id, game_id, rank),
            )

    async def existing_game_ids(self, game_ids: tuple[str, ...]) -> set[str]:
        """Return which of ``game_ids`` exist in the shared catalog.

        Membership is validated against ``games``, not against the caller's ``library_entries``. A
        collection has to keep rendering after the owner's library changes, so tying membership to current
        ownership would make collections decay.

        :raises psycopg.errors.InvalidTextRepresentation: If any id is not a well-formed UUID -- the cast
            below is what turns that into a catchable error instead of a mismatched-type query failure.
        """
        if not game_ids:
            return set()
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT game_id FROM games WHERE game_id = ANY(%s::uuid[])", (list(game_ids),))
            rows = await cur.fetchall()
        return {str(row[0]) for row in rows}

    #: Shared by every read of collection_definitions -- item_count is one subquery, computed here rather
    #: than making every caller (owner listing, profile listing, public share route) issue a second query
    #: just to answer "how big is this" (what ProfileDefinitionResponse was missing before 0019).
    _DEFINITION_COLUMNS = """
        cd.definition_id, cd.identity_sub, cd.name, cd.kind, cd.console_id, cd.genre_filter, cd.min_score,
        cd.aaa_tier_filter, cd.sort_order, cd.description, cd.include_inactive, cd.min_percent_completed,
        cd.filter_predicate, cd.visibility, cd.share_slug,
        (SELECT count(*) FROM collection_definition_items cdi WHERE cdi.definition_id = cd.definition_id)
    """

    async def list_definitions(self, identity_sub: str) -> list[CollectionDefinition]:
        """Return a user's saved collection definitions, newest first."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {self._DEFINITION_COLUMNS}
                FROM collection_definitions cd WHERE cd.identity_sub = %s ORDER BY cd.created_at DESC
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
                f"""
                SELECT {self._DEFINITION_COLUMNS}
                FROM collection_definitions cd WHERE cd.identity_sub = %s AND cd.definition_id = %s
                """,
                (identity_sub, definition_id),
            )
            row = await cur.fetchone()
        return self._to_definition(row) if row is not None else None

    async def get_definition_any_owner(self, definition_id: str) -> CollectionDefinition | None:
        """Return a definition regardless of whose it is -- unlike :meth:`get_definition`, not scoped to
        a caller. For the follow routes: a caller following a collection isn't its owner, so the
        ownership-scoped lookup would always miss. Callers here must still apply their own visibility
        check (a non-owner may only ever act on a non-``"private"`` result) -- this method does not do
        that filtering itself, unlike :meth:`get_definition_by_share_slug`, since an owner-facing caller
        (e.g. an admin/debug path) might legitimately need the private row too.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                f"SELECT {self._DEFINITION_COLUMNS} FROM collection_definitions cd WHERE cd.definition_id = %s",
                (definition_id,),
            )
            row = await cur.fetchone()
        return self._to_definition(row) if row is not None else None

    async def get_definition_by_share_slug(self, share_slug: str) -> CollectionDefinition | None:
        """Return a shared collection by its public link, or ``None`` if the slug is unknown *or* the
        collection is currently ``"private"`` -- the two are indistinguishable to an anonymous caller by
        design (see ``curator.public_collections_routes``), so a link that was shared and later made
        private stops working exactly like one that never existed.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {self._DEFINITION_COLUMNS}
                FROM collection_definitions cd WHERE cd.share_slug = %s AND cd.visibility != 'private'
                """,
                (share_slug,),
            )
            row = await cur.fetchone()
        return self._to_definition(row) if row is not None else None

    async def set_definition_visibility(
        self, identity_sub: str, definition_id: str, visibility: str
    ) -> CollectionDefinition | None:
        """Change a collection's visibility (``"private"``/``"unlisted"``/``"public"``).

        :returns: The updated definition, or ``None`` if it doesn't exist / belong to this user.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE collection_definitions SET visibility = %s, updated_at = now() "
                "WHERE identity_sub = %s AND definition_id = %s",
                (visibility, identity_sub, definition_id),
            )
        return await self.get_definition(identity_sub, definition_id)

    async def list_definition_items(self, definition_id: str) -> list[CollectionItem]:
        """Return a collection's stored membership, in rank order, joined with the shared catalog.

        Takes no ``identity_sub``, and must not: everything here is either cross-user or resolved from the
        *owner's* sub via ``collection_definitions``, never the viewer's. The caller's right to see the
        collection is decided by whoever resolved ``definition_id`` (the owner via :meth:`get_definition`
        today; a visibility check for another viewer later). Scoping any of this to the viewer would be
        actively wrong -- a shared collection would then show different artwork and different
        availability depending on who opened it.

        Cover art has two sources, tried in that order. ``psn_catalog_cache`` is the better one (a real
        store cover) but only has a row once PSN catalog enrichment has run for that game, which is not
        guaranteed. The entitlement artwork ingestion now captures is the fallback, so a collection made
        of freshly-imported games still renders. Reading a snapshot/library row belonging to some other
        user is deliberate and safe: artwork is a property of the game, not of any one owner's access to
        it -- the first subquery resolves ``title_id`` via any user's ``library_entries`` row for this
        game, and the second joins ``entitlement_snapshots`` through ``game_concepts`` by concept id
        alone, neither scoped to whose pull it came from.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                SELECT cdi.game_id, cdi.rank, g.canonical_title, g.franchise, gen.name, ge.aaa_tier,
                       ge.critical_score, ge.oc_score, ge.psn_rating,
                       COALESCE(
                           (
                               SELECT pcc.cover_image_url
                               FROM library_entries any_le
                               JOIN psn_catalog_cache pcc ON pcc.title_id = any_le.title_id
                               WHERE any_le.game_id = g.game_id AND pcc.cover_image_url IS NOT NULL
                               LIMIT 1
                           ),
                           (
                               SELECT COALESCE(es.title_image_url, es.game_icon_url, es.concept_icon_url)
                               FROM game_concepts gc
                               JOIN entitlement_snapshots es ON es.concept_id = gc.concept_id
                               WHERE gc.game_id = g.game_id
                                 AND COALESCE(es.title_image_url, es.game_icon_url, es.concept_icon_url) IS NOT NULL
                               LIMIT 1
                           )
                       ) AS cover_image_url,
                       COALESCE(le.is_active, false) AS owner_has_access
                FROM collection_definition_items cdi
                JOIN collection_definitions cd ON cd.definition_id = cdi.definition_id
                JOIN games g ON g.game_id = cdi.game_id
                LEFT JOIN game_enrichment ge ON ge.game_id = g.game_id
                LEFT JOIN genres gen ON gen.genre_id = ge.genre_id
                LEFT JOIN library_entries le
                       ON le.game_id = cdi.game_id AND le.identity_sub = cd.identity_sub
                WHERE cdi.definition_id = %s
                ORDER BY cdi.rank
                """,
                (definition_id,),
            )
            rows = await cur.fetchall()
        return [
            CollectionItem(
                game_id=str(row[0]),
                rank=row[1],
                title=row[2],
                franchise=row[3],
                genre=row[4],
                aaa_tier=row[5],
                critical_score=float(row[6]) if row[6] is not None else None,
                oc_score=float(row[7]) if row[7] is not None else None,
                psn_rating=float(row[8]) if row[8] is not None else None,
                cover_image_url=row[9],
                owner_has_access=bool(row[10]),
            )
            for row in rows
        ]

    async def update_definition(
        self, definition_id: str, *, name: str, description: str | None, game_ids: tuple[str, ...] | None = None
    ) -> None:
        """Overwrite a definition's name and description, and optionally its whole membership.

        Metadata and membership move in one connection block so a rename can never land while the item
        replacement it was issued with fails -- a collection called "PS5 shooters" holding the old RPG
        list is worse than the edit not happening at all.

        ``name`` and ``description`` are always written, so the caller resolves "unchanged" against the
        existing row before calling. ``game_ids`` is different: ``None`` means "leave membership alone",
        which is why it cannot collapse into the same always-write treatment.

        Membership replacement is wholesale rather than a diff. The request body *is* the new membership,
        so computing an add/remove delta would only create a way for the stored order to disagree with
        the requested one.

        ``updated_at`` is set explicitly: the column has a ``DEFAULT now()`` but no trigger, so without
        this it would record the creation time forever.

        Ownership is *not* checked here -- the caller establishes it with :meth:`get_definition` first.

        :raises psycopg.errors.UniqueViolation: If ``name`` collides with another of the owner's
            collections.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE collection_definitions
                SET name = %s, description = %s, updated_at = now()
                WHERE definition_id = %s
                """,
                (name, description, definition_id),
            )
            if game_ids is not None:
                await cur.execute("DELETE FROM collection_definition_items WHERE definition_id = %s", (definition_id,))
                await self._insert_items(cur, definition_id, game_ids)

    async def delete_definition(self, identity_sub: str, definition_id: str) -> bool:
        """Delete one of a user's collections, cascading to its items and runs.

        :returns: ``True`` if a row was deleted; ``False`` if the collection doesn't exist or isn't the
            caller's -- scoped in SQL, so those two cases stay indistinguishable to the caller.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM collection_definitions WHERE identity_sub = %s AND definition_id = %s",
                (identity_sub, definition_id),
            )
            return cur.rowcount > 0

    async def follow_collection(self, follower_sub: str, definition_id: str) -> None:
        """Follow a collection. Idempotent -- following one already followed is a no-op, matching
        ``curator.persistence.follow_repository.FollowRepository.follow``'s precedent."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO collection_follows (follower_sub, definition_id) VALUES (%s, %s)
                ON CONFLICT (follower_sub, definition_id) DO NOTHING
                """,
                (follower_sub, definition_id),
            )

    async def unfollow_collection(self, follower_sub: str, definition_id: str) -> bool:
        """Unfollow a collection.

        :returns: ``True`` if a row was actually removed -- callers use this to decide whether to write an
            audit-log entry, matching the account-follow precedent (no noise from a repeat no-op unfollow).
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM collection_follows WHERE follower_sub = %s AND definition_id = %s",
                (follower_sub, definition_id),
            )
            return cur.rowcount > 0

    async def is_following_collection(self, follower_sub: str, definition_id: str) -> bool:
        """Whether ``follower_sub`` currently follows this collection."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT 1 FROM collection_follows WHERE follower_sub = %s AND definition_id = %s",
                (follower_sub, definition_id),
            )
            row = await cur.fetchone()
        return row is not None

    async def collection_follower_count(self, definition_id: str) -> int:
        """How many users currently follow this collection."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM collection_follows WHERE definition_id = %s", (definition_id,))
            row = await cur.fetchone()
        assert row is not None
        return int(row[0])

    async def list_followed_collections(self, follower_sub: str) -> list[CollectionDefinition]:
        """Return every collection ``follower_sub`` follows, most recently followed first -- the
        "Collections I follow" view. Deliberately not filtered by the target's current visibility here:
        unfollowing is always available even if a collection was made private after the follow, so a
        stale-but-still-followed private collection would need its own explicit "no longer shared" state
        in the UI rather than silently vanishing from this list.
        """
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT {self._DEFINITION_COLUMNS}
                FROM collection_definitions cd
                JOIN collection_follows cf ON cf.definition_id = cd.definition_id
                WHERE cf.follower_sub = %s
                ORDER BY cf.created_at DESC
                """,
                (follower_sub,),
            )
            rows = await cur.fetchall()
        return [self._to_definition(row) for row in rows]

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
            description=row[9],
            include_inactive=bool(row[10]),
            min_percent_completed=row[11],
            filter_predicate=parse_predicate(row[12]) if row[12] is not None else None,
            visibility=row[13],
            share_slug=row[14],
            item_count=row[15],
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
