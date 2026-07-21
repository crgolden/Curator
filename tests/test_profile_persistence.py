"""Tests for ProfileRepository/FollowRepository, using the same hand-written fake pool/connection/cursor
pattern as test_enrichment_keys_repository.py/test_library_repository.py (no real database, no
unittest.mock)."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from curator.persistence.follow_repository import FollowRepository
from curator.persistence.profile_repository import ProfileRepository


class FakeCursor:
    def __init__(self, connection: FakeConnection) -> None:
        self._connection = connection

    async def execute(self, sql, params=None):
        self._connection.executed.append((sql, params))

    async def fetchone(self):
        return self._connection.fetchone_result

    async def fetchall(self):
        if self._connection.fetchall_results:
            return self._connection.fetchall_results.pop(0)
        return []

    @property
    def rowcount(self):
        return self._connection.rowcount

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, fetchone_result=None, fetchall_results=None, rowcount=0) -> None:
        self.executed: list[tuple[str, tuple | None]] = []
        self.fetchone_result = fetchone_result
        self.fetchall_results = list(fetchall_results or [])
        self.rowcount = rowcount

    def cursor(self):
        return FakeCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, fetchone_result=None, fetchall_results=None, rowcount=0) -> None:
        self._fetchone_result = fetchone_result
        self._fetchall_results = fetchall_results or []
        self._rowcount = rowcount
        self.connections: list[FakeConnection] = []

    def connection(self) -> FakeConnection:
        conn = FakeConnection(
            fetchone_result=self._fetchone_result,
            fetchall_results=list(self._fetchall_results),
            rowcount=self._rowcount,
        )
        self.connections.append(conn)
        return conn


# ---------------------------------------------------------------------------------------------------
# ProfileRepository
# ---------------------------------------------------------------------------------------------------


async def test_get_settings_returns_all_false_defaults_when_no_row():
    pool = FakePool(fetchone_result=None)
    repo = ProfileRepository(pool)

    settings = await repo.get_settings("sub-1")

    assert settings.is_public is False
    assert settings.show_library is False
    assert settings.show_collections is False
    assert settings.show_trophies is False
    assert settings.show_identity is False


async def test_get_settings_maps_row():
    row = (True, True, False, True, False)
    pool = FakePool(fetchone_result=row)
    repo = ProfileRepository(pool)

    settings = await repo.get_settings("sub-1")

    assert settings.is_public is True
    assert settings.show_library is True
    assert settings.show_collections is False
    assert settings.show_trophies is True
    assert settings.show_identity is False


async def test_upsert_settings_round_trips_every_field():
    pool = FakePool()
    repo = ProfileRepository(pool)

    await repo.upsert_settings(
        "sub-1",
        is_public=True,
        show_library=True,
        show_collections=True,
        show_trophies=False,
        show_identity=True,
    )

    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO user_profiles" in sql
    assert "ON CONFLICT (identity_sub) DO UPDATE" in sql
    assert params == ("sub-1", True, True, True, False, True)


# ---------------------------------------------------------------------------------------------------
# FollowRepository
# ---------------------------------------------------------------------------------------------------


async def test_follow_executes_insert_with_on_conflict_do_nothing():
    pool = FakePool()
    repo = FollowRepository(pool)

    await repo.follow("sub-a", "sub-b")

    sql, params = pool.connections[0].executed[0]
    assert "INSERT INTO follows" in sql
    assert "ON CONFLICT (follower_sub, followed_sub) DO NOTHING" in sql
    assert params == ("sub-a", "sub-b")


async def test_follow_is_idempotent_at_the_repository_call_level():
    pool = FakePool()
    repo = FollowRepository(pool)

    await repo.follow("sub-a", "sub-b")
    await repo.follow("sub-a", "sub-b")

    assert len(pool.connections) == 2  # two calls, each issuing its own ON CONFLICT DO NOTHING insert


async def test_unfollow_returns_true_when_a_row_was_removed():
    pool = FakePool(rowcount=1)
    repo = FollowRepository(pool)

    removed = await repo.unfollow("sub-a", "sub-b")

    assert removed is True
    sql, params = pool.connections[0].executed[0]
    assert "DELETE FROM follows" in sql
    assert params == ("sub-a", "sub-b")


async def test_unfollow_returns_false_when_nothing_was_removed():
    pool = FakePool(rowcount=0)
    repo = FollowRepository(pool)

    removed = await repo.unfollow("sub-a", "sub-b")

    assert removed is False


async def test_is_following_true_when_row_exists():
    pool = FakePool(fetchone_result=(1,))
    repo = FollowRepository(pool)

    assert await repo.is_following("sub-a", "sub-b") is True


async def test_is_following_false_when_no_row():
    pool = FakePool(fetchone_result=None)
    repo = FollowRepository(pool)

    assert await repo.is_following("sub-a", "sub-b") is False


async def test_follower_count_and_following_count():
    pool = FakePool(fetchone_result=(3,))
    repo = FollowRepository(pool)

    assert await repo.follower_count("sub-a") == 3
    assert await repo.following_count("sub-a") == 3


async def test_list_followers_returns_newest_first_and_respects_pagination_params():
    row1 = ("sub-b", datetime(2026, 2, 1, tzinfo=timezone.utc))
    row2 = ("sub-c", datetime(2026, 1, 1, tzinfo=timezone.utc))
    pool = FakePool(fetchall_results=[[row1, row2]])
    repo = FollowRepository(pool)

    edges = await repo.list_followers("sub-a", limit=10, offset=5)

    assert [edge.sub for edge in edges] == ["sub-b", "sub-c"]
    assert edges[0].followed_at == datetime(2026, 2, 1, tzinfo=timezone.utc)
    sql, params = pool.connections[0].executed[0]
    assert "ORDER BY created_at DESC" in sql
    assert "followed_sub = %s" in sql
    assert params == ("sub-a", 10, 5)


async def test_list_following_returns_newest_first_and_respects_pagination_params():
    row1 = ("sub-b", datetime(2026, 2, 1, tzinfo=timezone.utc))
    pool = FakePool(fetchall_results=[[row1]])
    repo = FollowRepository(pool)

    edges = await repo.list_following("sub-a", limit=25, offset=0)

    assert [edge.sub for edge in edges] == ["sub-b"]
    sql, params = pool.connections[0].executed[0]
    assert "ORDER BY created_at DESC" in sql
    assert "follower_sub = %s" in sql
    assert params == ("sub-a", 25, 0)


async def test_list_followers_coerces_uuid_column_to_str():
    """Regression test: psycopg returns a ``uuid.UUID`` instance (not ``str``) for a ``UUID`` column --
    reproduces a production bug where ``FollowEdge.sub`` stayed a ``UUID`` and failed
    ``FollowListEntryResponse`` pydantic validation (``sub: str``) with a real ``ValidationError``.
    """
    row = (UUID("9bd7af1c-7196-46f1-cadd-08dee5d60140"), datetime(2026, 2, 1, tzinfo=timezone.utc))
    pool = FakePool(fetchall_results=[[row]])
    repo = FollowRepository(pool)

    edges = await repo.list_followers("sub-a")

    assert edges[0].sub == "9bd7af1c-7196-46f1-cadd-08dee5d60140"
    assert isinstance(edges[0].sub, str)


async def test_list_following_coerces_uuid_column_to_str():
    row = (UUID("cb6d81d9-c670-425b-bfdb-08de789d89c0"), datetime(2026, 2, 1, tzinfo=timezone.utc))
    pool = FakePool(fetchall_results=[[row]])
    repo = FollowRepository(pool)

    edges = await repo.list_following("sub-a")

    assert edges[0].sub == "cb6d81d9-c670-425b-bfdb-08de789d89c0"
    assert isinstance(edges[0].sub, str)
