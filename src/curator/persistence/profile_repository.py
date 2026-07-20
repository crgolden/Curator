"""Data-access layer over ``user_profiles`` -- a user's own display-visibility toggles for the public
social-profile feature (see ``db/migrations/0005_user_profiles.sql``).

Same modular-repository convention as :class:`curator.persistence.enrichment_keys_repository
.EnrichmentKeysRepository`: deliberately separate from :class:`curator.persistence.repository.Repository`
(PSN links) and :class:`curator.persistence.follow_repository.FollowRepository` (the follow graph), even
though all three tables are queried together by ``curator.profile_routes``. Never enforces the
``show_* AND harvest_*`` gate itself -- that AND happens at the route layer, which is the only place that
also has access to ``psn_links``.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True, slots=True)
class ProfileSettings:
    """A user's own profile display-visibility toggles."""

    is_public: bool
    show_library: bool
    show_collections: bool
    show_trophies: bool
    show_identity: bool


_ALL_FALSE = ProfileSettings(
    is_public=False, show_library=False, show_collections=False, show_trophies=False, show_identity=False
)


class ProfileRepository:
    """DAO over ``user_profiles``.

    :param pool: The shared connection pool.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def get_settings(self, sub: str) -> ProfileSettings:
        """Return ``sub``'s profile display settings.

        Always answerable -- a user with no row (never visited profile settings) gets all-``False``
        defaults, never a 404; this is a status check, not a resource lookup, matching
        :meth:`curator.persistence.enrichment_keys_repository.EnrichmentKeysRepository.get_status`.

        :param sub: The Identity ``sub`` claim.
        """
        sql = (
            "SELECT is_public, show_library, show_collections, show_trophies, show_identity "
            "FROM user_profiles WHERE identity_sub = %s"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))
            row = await cur.fetchone()

        if row is None:
            return _ALL_FALSE
        return ProfileSettings(
            is_public=row[0],
            show_library=row[1],
            show_collections=row[2],
            show_trophies=row[3],
            show_identity=row[4],
        )

    async def upsert_settings(
        self,
        sub: str,
        *,
        is_public: bool,
        show_library: bool,
        show_collections: bool,
        show_trophies: bool,
        show_identity: bool,
    ) -> None:
        """Create or replace ``sub``'s profile display settings in one atomic upsert.

        No PSN-link precondition -- these toggles are meaningful even before/without a link (the AND-gate
        against ``psn_links.harvest_*`` happens at the route layer, at read time).

        :param sub: The Identity ``sub`` claim.
        :param is_public: Whether the profile is visible to other users at all.
        :param show_library: Whether the library section is shown (also requires the owner's own library
            data to exist).
        :param show_collections: Whether the collections section is shown.
        :param show_trophies: Whether the trophies section is shown (also requires
            ``psn_links.harvest_trophies``).
        :param show_identity: Whether the identity section is shown (also requires
            ``psn_links.harvest_identity``).
        """
        sql = (
            "INSERT INTO user_profiles "
            "(identity_sub, is_public, show_library, show_collections, show_trophies, show_identity) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (identity_sub) DO UPDATE SET "
            "is_public = EXCLUDED.is_public, "
            "show_library = EXCLUDED.show_library, "
            "show_collections = EXCLUDED.show_collections, "
            "show_trophies = EXCLUDED.show_trophies, "
            "show_identity = EXCLUDED.show_identity, "
            "updated_at = now()"
        )
        params = (sub, is_public, show_library, show_collections, show_trophies, show_identity)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
