"""Tests for QueueDepthMonitor, using a hand-written fake Service Bus administration client (no real
Azure connection)."""

from __future__ import annotations

import asyncio

import pytest

from curator import telemetry
from curator.jobs.queue_depth_monitor import QueueDepthMonitor


class FakeRuntimeProperties:
    def __init__(self, active_message_count, dead_letter_message_count):
        self.active_message_count = active_message_count
        self.dead_letter_message_count = dead_letter_message_count


class FakeAdminClient:
    """Stands in for azure.servicebus.management.ServiceBusAdministrationClient's sync admin surface."""

    def __init__(self, properties_by_queue=None, *, raises_for=()):
        self._properties_by_queue = properties_by_queue or {}
        self._raises_for = set(raises_for)
        self.calls: list[str] = []

    def get_queue_runtime_properties(self, queue_name, **kwargs):
        self.calls.append(queue_name)
        if queue_name in self._raises_for:
            raise RuntimeError(f"admin API unavailable for {queue_name}")
        return self._properties_by_queue[queue_name]


@pytest.fixture(autouse=True)
def _clear_queue_depth_gauges():
    """Every test starts from a clean slate -- these gauges are module-level state shared process-wide."""
    telemetry._QUEUE_ACTIVE_COUNTS.clear()
    telemetry._QUEUE_DEAD_LETTER_COUNTS.clear()
    yield
    telemetry._QUEUE_ACTIVE_COUNTS.clear()
    telemetry._QUEUE_DEAD_LETTER_COUNTS.clear()


async def test_poll_once_records_active_and_dead_letter_counts_per_queue():
    admin_client = FakeAdminClient(
        {
            "curator-library-refresh": FakeRuntimeProperties(active_message_count=2, dead_letter_message_count=1),
            "curator-enrichment": FakeRuntimeProperties(active_message_count=0, dead_letter_message_count=0),
        }
    )
    monitor = QueueDepthMonitor(admin_client, ["curator-library-refresh", "curator-enrichment"])

    await monitor.poll_once()

    assert telemetry._QUEUE_ACTIVE_COUNTS == {"curator-library-refresh": 2, "curator-enrichment": 0}
    assert telemetry._QUEUE_DEAD_LETTER_COUNTS == {"curator-library-refresh": 1, "curator-enrichment": 0}


async def test_poll_once_continues_past_a_failing_queue():
    admin_client = FakeAdminClient(
        {"curator-enrichment": FakeRuntimeProperties(active_message_count=5, dead_letter_message_count=0)},
        raises_for=("curator-library-refresh",),
    )
    monitor = QueueDepthMonitor(admin_client, ["curator-library-refresh", "curator-enrichment"])

    await monitor.poll_once()  # must not raise

    assert admin_client.calls == ["curator-library-refresh", "curator-enrichment"]
    assert "curator-library-refresh" not in telemetry._QUEUE_ACTIVE_COUNTS
    assert telemetry._QUEUE_ACTIVE_COUNTS == {"curator-enrichment": 5}


async def test_poll_once_skips_a_queue_reporting_no_counts():
    admin_client = FakeAdminClient(
        {"curator-library-refresh": FakeRuntimeProperties(active_message_count=None, dead_letter_message_count=None)}
    )
    monitor = QueueDepthMonitor(admin_client, ["curator-library-refresh"])

    await monitor.poll_once()

    assert telemetry._QUEUE_ACTIVE_COUNTS == {}
    assert telemetry._QUEUE_DEAD_LETTER_COUNTS == {}


async def test_start_and_stop_manage_the_background_task():
    admin_client = FakeAdminClient({"q": FakeRuntimeProperties(active_message_count=0, dead_letter_message_count=0)})
    monitor = QueueDepthMonitor(admin_client, ["q"], poll_interval_seconds=0.01)

    monitor.start()
    assert monitor._task is not None
    await asyncio.sleep(0.05)  # let at least one poll cycle run

    await monitor.stop()
    assert monitor._task is None
    assert len(admin_client.calls) >= 1
