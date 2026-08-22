"""Data-access layer over ``profile_link_sites`` and ``user_profile_links`` -- a user's declared profiles
on other PlayStation sites (see ``db/migrations/0042_user_profile_links.sql``).

Same modular-repository convention as :class:`curator.persistence.profile_repository.ProfileRepository`.

**The resolved URL is built here, from the site's stored template, and never accepted from a caller.**
That is the reason this feature stores a site key plus a handle rather than a URL: the only user-supplied
value is the handle, and it never reaches an ``href`` except through a template no user can write.
"""

from __future__ import annotations

from dataclasses import dataclass

from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True, slots=True)
class ProfileLinkSite:
    """One allowlisted PlayStation site a user may link a profile on."""

    site_key: str
    display_name: str
    url_template: str
    sort_order: int


@dataclass(frozen=True, slots=True)
class ProfileLink:
    """A user's declared profile on one allowlisted site, with its URL already resolved."""

    site_key: str
    display_name: str
    handle: str
    url: str


class ProfileLinkRepository:
    """DAO over ``profile_link_sites`` and ``user_profile_links``.

    :param pool: The shared connection pool.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def list_sites(self) -> list[ProfileLinkSite]:
        """Return every allowlisted site, in display order.

        Backs both the settings form's options and the route layer's validation of an incoming
        ``site_key`` -- a key absent from this table is rejected with a 400 rather than reaching the
        foreign key as a 23503.
        """
        sql = "SELECT site_key, display_name, url_template, sort_order FROM profile_link_sites ORDER BY sort_order"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql)
            rows = await cur.fetchall()
        return [
            ProfileLinkSite(site_key=row[0], display_name=row[1], url_template=row[2], sort_order=row[3])
            for row in rows
        ]

    async def list_for_user(self, sub: str) -> list[ProfileLink]:
        """Return ``sub``'s declared profile links, in the sites' display order.

        :param sub: The Identity ``sub`` claim.
        """
        sql = (
            "SELECT s.site_key, s.display_name, l.handle, s.url_template "
            "FROM user_profile_links l JOIN profile_link_sites s ON s.site_key = l.site_key "
            "WHERE l.identity_sub = %s ORDER BY s.sort_order"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))
            rows = await cur.fetchall()
        return [
            ProfileLink(
                site_key=row[0],
                display_name=row[1],
                handle=row[2],
                url=row[3].replace("{handle}", row[2]),
            )
            for row in rows
        ]

    async def upsert_link(self, sub: str, site_key: str, handle: str) -> None:
        """Create or replace ``sub``'s handle for one site.

        :param sub: The Identity ``sub`` claim.
        :param site_key: An allowlisted key from :meth:`list_sites`.
        :param handle: The user's handle on that site.
        """
        sql = (
            "INSERT INTO user_profile_links (identity_sub, site_key, handle) VALUES (%s, %s, %s) "
            "ON CONFLICT (identity_sub, site_key) DO UPDATE SET handle = EXCLUDED.handle"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub, site_key, handle))

    async def delete_link(self, sub: str, site_key: str) -> None:
        """Remove ``sub``'s link for one site. Idempotent.

        :param sub: The Identity ``sub`` claim.
        :param site_key: The site to unlink.
        """
        sql = "DELETE FROM user_profile_links WHERE identity_sub = %s AND site_key = %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub, site_key))
