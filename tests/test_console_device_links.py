"""Tests for console <-> PSN-registered-device links, using hand-written fake psycopg objects."""

from __future__ import annotations

from curator.collections.repository import CollectionsRepository


class FakeCursor:
    def __init__(self, connection):
        self._connection = connection
        self.rowcount = connection.rowcount

    async def execute(self, sql, params=None):
        self._connection.executed.append((sql, params))

    async def fetchone(self):
        return None

    async def fetchall(self):
        if self._connection.fetchall_results:
            return self._connection.fetchall_results.pop(0)
        return []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakeConnection:
    def __init__(self, fetchall_results=None, rowcount=0):
        self.executed: list[tuple[str, tuple | None]] = []
        self.fetchall_results = list(fetchall_results or [])
        self.rowcount = rowcount

    def cursor(self):
        return FakeCursor(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class FakePool:
    def __init__(self, fetchall_results=None, rowcount=0):
        self._fetchall_results = fetchall_results or []
        self._rowcount = rowcount
        self.connections: list[FakeConnection] = []

    def connection(self):
        conn = FakeConnection(fetchall_results=list(self._fetchall_results), rowcount=self._rowcount)
        self.connections.append(conn)
        return conn


async def test_links_are_returned_keyed_by_device_for_annotating_a_device_list():
    pool = FakePool(fetchall_results=[[("dev-1", "console-a"), ("dev-2", "console-b")]])
    repository = CollectionsRepository(pool)

    links = await repository.list_console_device_links("sub-a")

    assert links == {"dev-1": "console-a", "dev-2": "console-b"}


async def test_linking_clears_both_sides_before_inserting():
    pool = FakePool()
    repository = CollectionsRepository(pool)

    await repository.link_console_device("sub-a", "console-a", "dev-1")

    delete_sql, delete_params = pool.connections[0].executed[0]
    assert delete_sql.strip().startswith("DELETE FROM console_device_links")
    assert "console_id = %s OR device_id = %s" in delete_sql, (
        "either uniqueness rule can be the one that fires, so re-linking must clear both sides"
    )
    assert delete_params == ("sub-a", "console-a", "dev-1")

    insert_sql, insert_params = pool.connections[0].executed[1]
    assert insert_sql.strip().startswith("INSERT INTO console_device_links")
    assert insert_params == ("sub-a", "console-a", "dev-1")


async def test_linking_happens_in_one_connection_so_the_clear_and_insert_cannot_separate():
    pool = FakePool()
    repository = CollectionsRepository(pool)

    await repository.link_console_device("sub-a", "console-a", "dev-1")

    assert len(pool.connections) == 1, "a clear that lands without its insert would silently unlink a console"
    assert len(pool.connections[0].executed) == 2


async def test_unlinking_is_scoped_to_the_caller_and_reports_whether_a_link_existed():
    pool = FakePool(rowcount=1)
    repository = CollectionsRepository(pool)

    assert await repository.unlink_console_device("sub-a", "console-a") is True
    sql, params = pool.connections[0].executed[0]
    assert "identity_sub = %s AND console_id = %s" in sql
    assert params == ("sub-a", "console-a")


async def test_unlinking_a_console_with_no_link_reports_false():
    pool = FakePool(rowcount=0)
    repository = CollectionsRepository(pool)

    assert await repository.unlink_console_device("sub-a", "console-a") is False
