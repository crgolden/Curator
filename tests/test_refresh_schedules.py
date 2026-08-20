"""Tests for GET/PUT/DELETE /me/refresh-schedule and QueuePublisher.publish_scheduled_library_refresh --
create_app wired with a hand-written FakeRefreshSchedulesRepository, same DI-seam style as
test_preferences_routes.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from curator.app import create_app
from curator.jobs.queue_publisher import QueuePublisher
from curator.persistence.crypto import TokenCrypto
from curator.persistence.refresh_schedules_repository import RefreshSchedule, next_run_after
from test_routes import (
    EMAIL,
    SUB,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _seed_link,
)

_NOW = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


class FakeRefreshSchedulesRepository:
    """Stands in for RefreshSchedulesRepository: in-memory dict of sub -> RefreshSchedule."""

    def __init__(self) -> None:
        self.schedules: dict[str, RefreshSchedule] = {}
        self.delete_calls: list[str] = []

    async def get(self, sub):
        return self.schedules.get(sub)

    async def upsert(self, sub, *, cadence, ps_plus_watch, next_run_at):
        schedule = RefreshSchedule(
            identity_sub=sub,
            cadence=cadence,
            ps_plus_watch=ps_plus_watch,
            next_run_at=next_run_at,
            last_run_at=None,
            consecutive_failures=0,
            paused_reason=None,
        )
        self.schedules[sub] = schedule
        return schedule

    async def delete(self, sub):
        self.schedules.pop(sub, None)
        self.delete_calls.append(sub)


class FakeQueuePublisher:
    """Records scheduled-refresh publishes; the routes only ever call this one method."""

    def __init__(self) -> None:
        self.scheduled_calls: list[tuple[str, datetime]] = []

    async def publish_scheduled_library_refresh(self, identity_sub, scheduled_for):
        self.scheduled_calls.append((identity_sub, scheduled_for))


class FakeSender:
    """Records Service Bus sends, distinguishing immediate from scheduled."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.scheduled: list[tuple[str, datetime]] = []

    async def send_messages(self, message):
        self.sent.append(str(message))

    async def schedule_messages(self, messages, schedule_time_utc):
        self.scheduled.append((str(messages), schedule_time_utc))
        return [1]


class FakeJobRunsRepository:
    """Records job_runs row creation, so a test can assert a publish path does not create one."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str, str | None]] = []

    async def create(self, run_id, kind, identity_sub=None):
        self.created.append((run_id, kind, identity_sub))


def _build(*, linked: bool, schedules=None, publisher=None):
    settings = _make_settings()
    repository = FakeRepository()
    if linked:
        _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), SUB)
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(sub=SUB, email=EMAIL))
    app = create_app(
        settings,
        repository=repository,
        token_validator=validator,
        refresh_schedules_repository=schedules or FakeRefreshSchedulesRepository(),
    )
    app.state.queue_publisher = publisher if publisher is not None else FakeQueuePublisher()
    return TestClient(app), app.state.refresh_schedules_repository, app.state.queue_publisher


def test_next_run_after_weekly_is_seven_days_out():
    assert next_run_after("weekly", now=_NOW) == _NOW + timedelta(days=7)


def test_next_run_after_monthly_is_thirty_days_out():
    assert next_run_after("monthly", now=_NOW) == _NOW + timedelta(days=30)


def test_get_schedule_without_a_psn_link_is_404():
    client, _, _ = _build(linked=False)
    assert client.get("/me/refresh-schedule", headers=_bearer("valid-token")).status_code == 404


def test_get_schedule_when_none_is_configured_is_404():
    client, _, _ = _build(linked=True)
    assert client.get("/me/refresh-schedule", headers=_bearer("valid-token")).status_code == 404


def test_put_schedule_without_a_psn_link_is_404_and_stores_nothing():
    client, schedules, publisher = _build(linked=False)

    response = client.put(
        "/me/refresh-schedule", json={"cadence": "weekly", "ps_plus_watch": False}, headers=_bearer("valid-token")
    )

    assert response.status_code == 404
    assert schedules.schedules == {}
    assert publisher.scheduled_calls == []


def test_put_schedule_stores_it_and_publishes_the_first_run():
    client, schedules, publisher = _build(linked=True)

    response = client.put(
        "/me/refresh-schedule", json={"cadence": "weekly", "ps_plus_watch": True}, headers=_bearer("valid-token")
    )

    assert response.status_code == 200
    body = response.json()
    assert body["cadence"] == "weekly"
    assert body["ps_plus_watch"] is True
    assert body["consecutive_failures"] == 0
    assert body["paused_reason"] is None

    assert schedules.schedules[SUB].cadence == "weekly"
    published_sub, published_at = publisher.scheduled_calls[0]
    assert published_sub == SUB
    assert published_at == schedules.schedules[SUB].next_run_at


def test_put_schedule_is_refused_when_no_queue_is_configured_rather_than_storing_a_dead_schedule():
    client, schedules, _ = _build(linked=True)
    client.app.state.queue_publisher = None

    response = client.put("/me/refresh-schedule", json={"cadence": "weekly"}, headers=_bearer("valid-token"))

    assert response.status_code == 503
    assert schedules.schedules == {}


def test_put_schedule_rejects_an_unknown_cadence():
    client, schedules, _ = _build(linked=True)

    response = client.put("/me/refresh-schedule", json={"cadence": "hourly"}, headers=_bearer("valid-token"))

    assert response.status_code == 422
    assert schedules.schedules == {}


def test_put_schedule_replaces_an_existing_one_and_publishes_again():
    client, schedules, publisher = _build(linked=True)
    client.put("/me/refresh-schedule", json={"cadence": "weekly"}, headers=_bearer("valid-token"))

    response = client.put(
        "/me/refresh-schedule", json={"cadence": "monthly", "ps_plus_watch": True}, headers=_bearer("valid-token")
    )

    assert response.status_code == 200
    assert schedules.schedules[SUB].cadence == "monthly"
    assert schedules.schedules[SUB].ps_plus_watch is True
    assert len(publisher.scheduled_calls) == 2


def test_delete_schedule_removes_it():
    client, schedules, _ = _build(linked=True)
    client.put("/me/refresh-schedule", json={"cadence": "weekly"}, headers=_bearer("valid-token"))

    response = client.delete("/me/refresh-schedule", headers=_bearer("valid-token"))

    assert response.status_code == 204
    assert schedules.schedules == {}
    assert schedules.delete_calls == [SUB]


async def test_publish_scheduled_library_refresh_creates_no_job_run_row():
    sender = FakeSender()
    job_runs = FakeJobRunsRepository()
    publisher = QueuePublisher(
        library_refresh_sender=FakeSender(),
        enrichment_sender=FakeSender(),
        scheduled_refresh_sender=sender,
        job_runs_repository=job_runs,
    )

    await publisher.publish_scheduled_library_refresh(SUB, _NOW)

    assert job_runs.created == []
    assert sender.sent == []


async def test_publish_scheduled_library_refresh_defers_to_the_requested_time_and_echoes_it():
    sender = FakeSender()
    publisher = QueuePublisher(
        library_refresh_sender=FakeSender(),
        enrichment_sender=FakeSender(),
        scheduled_refresh_sender=sender,
        job_runs_repository=FakeJobRunsRepository(),
    )

    await publisher.publish_scheduled_library_refresh(SUB, _NOW)

    body, schedule_time = sender.scheduled[0]
    assert schedule_time == _NOW
    assert json.loads(body) == {"identity_sub": SUB, "scheduled_for": _NOW.isoformat()}


async def test_publish_scheduled_library_refresh_without_a_configured_queue_raises():
    publisher = QueuePublisher(
        library_refresh_sender=FakeSender(),
        enrichment_sender=FakeSender(),
        job_runs_repository=FakeJobRunsRepository(),
    )

    try:
        await publisher.publish_scheduled_library_refresh(SUB, _NOW)
    except RuntimeError:
        return
    raise AssertionError("expected a RuntimeError when no scheduled-refresh sender is configured")
