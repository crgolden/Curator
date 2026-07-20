"""Data-access layer over ``follows`` -- the directed follow graph backing the public social-profile
feature (see ``db/migrations/0006_follows.sql``).

First-party Curator data, not PSN-derived -- this is why ``curator.profile_routes`` never gates follow/
follower/following operations on a target's ``user_profiles.is_public`` flag the way it gates PSN-derived
sections. Same modular-repository convention as every other DAO in this codebase: raw parameterized SQL,
frozen dataclass results, backed by the shared ``psycopg_pool.AsyncConnectionPool``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg_pool import AsyncConnectionPool


@dataclass(frozen=True, slots=True)
class FollowEdge:
    """One row of a followers/following list -- the *other* user's sub plus when the edge was created."""

    sub: str
    followed_at: datetime


class FollowRepository:
    """DAO over ``follows``.

    :param pool: The shared connection pool.
    """

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def follow(self, follower_sub: str, followed_sub: str) -> None:
        """Create the ``follower_sub -> followed_sub`` edge, if it doesn't already exist.

        Idempotent via ``ON CONFLICT DO NOTHING`` -- the composite primary key on ``(follower_sub,
        followed_sub)`` makes a repeat follow a no-op rather than an error. Does not itself reject a
        self-follow (the ``follows_no_self_follow`` CHECK constraint would raise undisguised); the route
        layer pre-checks ``follower_sub != followed_sub`` for a clean 400 before this is ever called.

        :param follower_sub: The Identity ``sub`` of the user doing the following.
        :param followed_sub: The Identity ``sub`` of the user being followed.
        """
        sql = (
            "INSERT INTO follows (follower_sub, followed_sub) VALUES (%s, %s) "
            "ON CONFLICT (follower_sub, followed_sub) DO NOTHING"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (follower_sub, followed_sub))

    async def unfollow(self, follower_sub: str, followed_sub: str) -> bool:
        """Remove the ``follower_sub -> followed_sub`` edge, if it exists.

        Idempotent -- unfollowing a user not currently followed is a no-op, not an error.

        :param follower_sub: The Identity ``sub`` of the user doing the unfollowing.
        :param followed_sub: The Identity ``sub`` of the user being unfollowed.
        :returns: ``True`` if a row was actually removed, ``False`` if there was nothing to remove --
            callers use this to decide whether to write an audit log entry.
        """
        sql = "DELETE FROM follows WHERE follower_sub = %s AND followed_sub = %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (follower_sub, followed_sub))
            return cur.rowcount > 0

    async def is_following(self, follower_sub: str, followed_sub: str) -> bool:
        """Check whether ``follower_sub`` currently follows ``followed_sub``.

        :param follower_sub: The Identity ``sub`` of the potential follower.
        :param followed_sub: The Identity ``sub`` of the potential followed user.
        """
        sql = "SELECT 1 FROM follows WHERE follower_sub = %s AND followed_sub = %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (follower_sub, followed_sub))
            row = await cur.fetchone()
        return row is not None

    async def follower_count(self, sub: str) -> int:
        """Count how many users follow ``sub``.

        :param sub: The Identity ``sub`` whose followers are being counted.
        """
        sql = "SELECT count(*) FROM follows WHERE followed_sub = %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))
            row = await cur.fetchone()
        return int(row[0]) if row is not None else 0

    async def following_count(self, sub: str) -> int:
        """Count how many users ``sub`` follows.

        :param sub: The Identity ``sub`` whose following-count is being computed.
        """
        sql = "SELECT count(*) FROM follows WHERE follower_sub = %s"
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub,))
            row = await cur.fetchone()
        return int(row[0]) if row is not None else 0

    async def list_followers(self, sub: str, *, limit: int = 100, offset: int = 0) -> list[FollowEdge]:
        """List the users following ``sub``, newest edge first.

        :param sub: The Identity ``sub`` whose followers are being listed.
        :param limit: Maximum number of edges to return.
        :param offset: Number of edges to skip, for pagination.
        """
        sql = (
            "SELECT follower_sub, created_at FROM follows WHERE followed_sub = %s "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub, limit, offset))
            rows = await cur.fetchall()
        return [FollowEdge(sub=row[0], followed_at=row[1]) for row in rows]

    async def list_following(self, sub: str, *, limit: int = 100, offset: int = 0) -> list[FollowEdge]:
        """List the users ``sub`` follows, newest edge first.

        :param sub: The Identity ``sub`` whose following-list is being listed.
        :param limit: Maximum number of edges to return.
        :param offset: Number of edges to skip, for pagination.
        """
        sql = (
            "SELECT followed_sub, created_at FROM follows WHERE follower_sub = %s "
            "ORDER BY created_at DESC LIMIT %s OFFSET %s"
        )
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, (sub, limit, offset))
            rows = await cur.fetchall()
        return [FollowEdge(sub=row[0], followed_at=row[1]) for row in rows]
