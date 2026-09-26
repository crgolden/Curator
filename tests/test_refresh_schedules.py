"""Tests for GET/PUT/DELETE /me/refresh-schedule and QueuePublisher.publish_scheduled_library_refresh --
create_app wired with a hand-written FakeRefreshSchedulesRepository, same DI-seam style as
test_preferences_routes.py.
"""

from __future__ import annotations

import json
import random
from datetime import datetime, timedelta, timezone
from typing import get_args

import pytest
from fastapi.testclient import TestClient

from curator import refresh_schedules_routes
from curator.app import create_app
from curator.deps import PREFERENCE_NOT_LINKED_DETAIL
from curator.jobs.queue_publisher import IDENTITY_SUB_FIELD, SCHEDULED_FOR_FIELD, QueuePublisher
from curator.persistence.crypto import TokenCrypto
from curator.persistence.refresh_schedules_repository import (
    CADENCE_DAILY,
    CADENCE_MONTHLY,
    CADENCE_WEEKLY,
    Cadence,
    RefreshSchedule,
    next_run_after,
)
from curator.refresh_schedules_routes import (
    NO_QUEUE_DETAIL,
    NO_SCHEDULE_DETAIL,
    RefreshScheduleRequest,
    RefreshScheduleResponse,
)
from test_routes import (
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _path,
    _seed_link,
)
from test_values import (
    lowercase_token,
    new_email_address,
    new_identity_sub,
    new_opaque_token,
    new_small_count,
    new_utc_instant,
)

_CADENCES: tuple[Cadence, ...] = get_args(Cadence)


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
        return [new_small_count()]


class FakeJobRunsRepository:
    """Records job_runs row creation, so a test can assert a publish path does not create one."""

    def __init__(self) -> None:
        self.created: list[tuple[str, str, str | None]] = []

    async def create(self, run_id, kind, identity_sub=None):
        self.created.append((run_id, kind, identity_sub))


class _Caller:
    def __init__(self) -> None:
        self.sub = new_identity_sub()
        self.token = new_opaque_token()


def _build(caller: _Caller, *, linked: bool, schedules=None, publisher=None):
    settings = _make_settings()
    repository = FakeRepository()
    if linked:
        _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), caller.sub)
    validator = FakeTokenValidator()
    validator.register(caller.token, _claims(sub=caller.sub, email=new_email_address()))
    app = create_app(
        settings,
        repository=repository,
        token_validator=validator,
        refresh_schedules_repository=schedules or FakeRefreshSchedulesRepository(),
    )
    app.state.queue_publisher = publisher if publisher is not None else FakeQueuePublisher()
    return TestClient(app), app.state.refresh_schedules_repository, app.state.queue_publisher


def _schedule_path(client: TestClient) -> str:
    return _path(client, refresh_schedules_routes.set_refresh_schedule)


def _schedule_body(cadence: Cadence, ps_plus_watch: bool = False) -> dict[str, object]:
    return RefreshScheduleRequest(cadence=cadence, ps_plus_watch=ps_plus_watch).model_dump()


def _body_with_an_unknown_cadence() -> dict[str, object]:
    cadence = random.choice(_CADENCES)
    body = _schedule_body(cadence)
    return {key: lowercase_token() if value == cadence else value for key, value in body.items()}


def _published_scheduler() -> tuple[QueuePublisher, FakeSender, FakeJobRunsRepository]:
    sender = FakeSender()
    job_runs = FakeJobRunsRepository()
    publisher = QueuePublisher(
        library_refresh_sender=FakeSender(),
        enrichment_sender=FakeSender(),
        scheduled_refresh_sender=sender,
        job_runs_repository=job_runs,
    )
    return publisher, sender, job_runs


def test_next_run_after_daily_is_one_day_out():
    now = new_utc_instant()

    next_run_at = next_run_after(CADENCE_DAILY, now=now)

    assert next_run_at == now + timedelta(days=1)


def test_next_run_after_weekly_is_seven_days_out():
    now = new_utc_instant()

    next_run_at = next_run_after(CADENCE_WEEKLY, now=now)

    assert next_run_at == now + timedelta(days=7)


def test_next_run_after_monthly_is_thirty_days_out():
    now = new_utc_instant()

    next_run_at = next_run_after(CADENCE_MONTHLY, now=now)

    assert next_run_at == now + timedelta(days=30)


def test_get_schedule_without_a_psn_link_is_404():
    caller = _Caller()
    client, _, _ = _build(caller, linked=False)

    response = client.get(_path(client, refresh_schedules_routes.get_refresh_schedule), headers=_bearer(caller.token))

    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL


def test_get_schedule_when_none_is_configured_is_404():
    caller = _Caller()
    client, _, _ = _build(caller, linked=True)

    response = client.get(_path(client, refresh_schedules_routes.get_refresh_schedule), headers=_bearer(caller.token))

    assert response.status_code == 404
    assert response.json()["detail"] == NO_SCHEDULE_DETAIL


def test_put_schedule_without_a_psn_link_is_404_and_stores_nothing():
    caller = _Caller()
    client, schedules, publisher = _build(caller, linked=False)

    response = client.put(
        _schedule_path(client), json=_schedule_body(random.choice(_CADENCES)), headers=_bearer(caller.token)
    )

    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL
    assert schedules.schedules == {}
    assert publisher.scheduled_calls == []


def test_put_schedule_stores_it_and_publishes_the_first_run():
    caller = _Caller()
    client, schedules, publisher = _build(caller, linked=True)
    cadence = random.choice(_CADENCES)

    response = client.put(
        _schedule_path(client), json=_schedule_body(cadence, ps_plus_watch=True), headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    stored = schedules.schedules[caller.sub]
    assert RefreshScheduleResponse.model_validate(response.json()) == RefreshScheduleResponse(
        cadence=cadence,
        ps_plus_watch=True,
        next_run_at=stored.next_run_at,
        last_run_at=None,
        consecutive_failures=0,
        paused_reason=None,
    )
    assert stored.cadence == cadence
    assert publisher.scheduled_calls == [(caller.sub, stored.next_run_at)]


def test_put_schedule_is_refused_when_no_queue_is_configured_rather_than_storing_a_dead_schedule():
    caller = _Caller()
    client, schedules, _ = _build(caller, linked=True)
    client.app.state.queue_publisher = None

    response = client.put(
        _schedule_path(client), json=_schedule_body(random.choice(_CADENCES)), headers=_bearer(caller.token)
    )

    assert response.status_code == 503
    assert response.json()["detail"] == NO_QUEUE_DETAIL
    assert schedules.schedules == {}


def test_put_schedule_accepts_a_daily_cadence_and_schedules_the_first_run_a_day_out():
    caller = _Caller()
    client, schedules, publisher = _build(caller, linked=True)

    response = client.put(_schedule_path(client), json=_schedule_body(CADENCE_DAILY), headers=_bearer(caller.token))

    assert response.status_code == 200
    assert RefreshScheduleResponse.model_validate(response.json()).cadence == CADENCE_DAILY
    assert schedules.schedules[caller.sub].cadence == CADENCE_DAILY
    _, published_at = publisher.scheduled_calls[0]
    assert published_at - datetime.now(timezone.utc) < timedelta(days=1)
    assert published_at - datetime.now(timezone.utc) > timedelta(hours=23)


def test_put_schedule_rejects_an_unknown_cadence():
    caller = _Caller()
    client, schedules, _ = _build(caller, linked=True)

    response = client.put(_schedule_path(client), json=_body_with_an_unknown_cadence(), headers=_bearer(caller.token))

    assert response.status_code == 422
    assert schedules.schedules == {}


def test_put_schedule_replaces_an_existing_one_and_publishes_again():
    caller = _Caller()
    client, schedules, publisher = _build(caller, linked=True)
    client.put(_schedule_path(client), json=_schedule_body(CADENCE_WEEKLY), headers=_bearer(caller.token))
    first_next_run_at = schedules.schedules[caller.sub].next_run_at

    response = client.put(
        _schedule_path(client), json=_schedule_body(CADENCE_MONTHLY, ps_plus_watch=True), headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    stored = schedules.schedules[caller.sub]
    assert stored.cadence == CADENCE_MONTHLY
    assert stored.ps_plus_watch is True
    assert publisher.scheduled_calls == [(caller.sub, first_next_run_at), (caller.sub, stored.next_run_at)]


def test_put_schedule_with_the_same_cadence_keeps_the_next_run_and_publishes_nothing_new():
    caller = _Caller()
    client, schedules, publisher = _build(caller, linked=True)
    cadence = random.choice(_CADENCES)
    client.put(_schedule_path(client), json=_schedule_body(cadence), headers=_bearer(caller.token))
    first_next_run_at = schedules.schedules[caller.sub].next_run_at

    response = client.put(
        _schedule_path(client), json=_schedule_body(cadence, ps_plus_watch=True), headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    assert schedules.schedules[caller.sub].ps_plus_watch is True
    assert schedules.schedules[caller.sub].next_run_at == first_next_run_at
    assert publisher.scheduled_calls == [(caller.sub, first_next_run_at)]


def test_put_schedule_on_a_paused_chain_restarts_it_from_now_even_with_the_same_cadence():
    caller = _Caller()
    cadence = random.choice(_CADENCES)
    schedules = FakeRefreshSchedulesRepository()
    schedules.schedules[caller.sub] = RefreshSchedule(
        identity_sub=caller.sub,
        cadence=cadence,
        ps_plus_watch=False,
        next_run_at=new_utc_instant(),
        last_run_at=None,
        consecutive_failures=new_small_count(),
        paused_reason=lowercase_token(),
    )
    client, schedules, publisher = _build(caller, linked=True, schedules=schedules)

    response = client.put(_schedule_path(client), json=_schedule_body(cadence), headers=_bearer(caller.token))

    assert response.status_code == 200
    restarted_next_run_at = schedules.schedules[caller.sub].next_run_at
    assert restarted_next_run_at > datetime.now(timezone.utc)
    assert publisher.scheduled_calls == [(caller.sub, restarted_next_run_at)]


def test_delete_schedule_removes_it():
    caller = _Caller()
    client, schedules, _ = _build(caller, linked=True)
    client.put(_schedule_path(client), json=_schedule_body(random.choice(_CADENCES)), headers=_bearer(caller.token))

    response = client.delete(
        _path(client, refresh_schedules_routes.delete_refresh_schedule), headers=_bearer(caller.token)
    )

    assert response.status_code == 204
    assert schedules.schedules == {}
    assert schedules.delete_calls == [caller.sub]


async def test_publish_scheduled_library_refresh_creates_no_job_run_row():
    publisher, sender, job_runs = _published_scheduler()

    await publisher.publish_scheduled_library_refresh(new_identity_sub(), new_utc_instant())

    assert job_runs.created == []
    assert sender.sent == []


async def test_publish_scheduled_library_refresh_defers_to_the_requested_time_and_echoes_it():
    publisher, sender, _job_runs = _published_scheduler()
    identity_sub = new_identity_sub()
    scheduled_for = new_utc_instant()

    await publisher.publish_scheduled_library_refresh(identity_sub, scheduled_for)

    body, schedule_time = sender.scheduled[0]
    assert schedule_time == scheduled_for
    assert json.loads(body) == {IDENTITY_SUB_FIELD: identity_sub, SCHEDULED_FOR_FIELD: scheduled_for.isoformat()}


async def test_publish_scheduled_library_refresh_without_a_configured_queue_raises():
    publisher = QueuePublisher(
        library_refresh_sender=FakeSender(),
        enrichment_sender=FakeSender(),
        job_runs_repository=FakeJobRunsRepository(),
    )

    with pytest.raises(RuntimeError):
        await publisher.publish_scheduled_library_refresh(new_identity_sub(), new_utc_instant())
