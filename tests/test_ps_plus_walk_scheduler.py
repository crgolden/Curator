"""Tests for the in-process weekly PS Plus walk scheduler, using fake walkers and histories."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from curator.jobs.ps_plus_walk_scheduler import DEFAULT_WALK_INTERVAL, PsPlusWalkScheduler


class FakeWalker:
    def __init__(self, *, fails=False):
        self.calls = 0
        self._fails = fails

    async def walk_all(self, *, max_pages_per_category=None):
        self.calls += 1
        if self._fails:
            raise RuntimeError("gateway down")
        return []


class FakeHistory:
    def __init__(self, latest):
        self._latest = latest

    async def latest_walk_started_at(self):
        return self._latest


_NOW = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)


def _scheduler(walker, latest):
    return PsPlusWalkScheduler(walker, FakeHistory(latest), clock=lambda: _NOW)


async def test_walks_when_no_walk_has_ever_run():
    walker = FakeWalker()

    ran = await _scheduler(walker, None).check_once()

    assert ran
    assert walker.calls == 1


async def test_does_not_walk_when_the_last_walk_is_younger_than_the_interval():
    walker = FakeWalker()

    ran = await _scheduler(walker, _NOW - DEFAULT_WALK_INTERVAL + timedelta(hours=1)).check_once()

    assert not ran
    assert walker.calls == 0


async def test_walks_when_the_last_walk_is_at_least_the_interval_old():
    walker = FakeWalker()

    ran = await _scheduler(walker, _NOW - DEFAULT_WALK_INTERVAL).check_once()

    assert ran
    assert walker.calls == 1


async def test_a_failed_walk_does_not_escape_the_loop():
    walker = FakeWalker(fails=True)

    ran = await _scheduler(walker, None).check_once()

    assert not ran
    assert walker.calls == 1


async def test_stop_before_start_is_a_no_op():
    await _scheduler(FakeWalker(), None).stop()
