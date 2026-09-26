"""Tests for POST /enrichment/runs, admin-scoped, using create_app() with a fake QueuePublisher."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from curator import enrichment_routes
from curator.app import create_app
from curator.enrichment_routes import (
    CANCELLED_BY_ADMIN,
    ENRICHMENT_RUN_NOUN,
    EnrichmentRunResponse,
    EnrichmentRunStatusResponse,
)
from curator.jobs.repository import (
    JOB_KIND_ENRICHMENT,
    JOB_KIND_LIBRARY_REFRESH,
    JOB_STATUS_CANCELLED,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    TERMINAL_STATUSES,
)
from curator.jobs.staleness import STALE_RUN_THRESHOLD, lease_lapsed_reason, no_progress_reason
from curator.persistence.crypto import TokenCrypto
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _path
from test_values import lowercase_token, new_opaque_token, new_result_summary, new_run_id, new_short_interval

_QUEUED_STATUS = "queued"


class FakePublisher:
    def __init__(self):
        self.run_id = new_run_id()
        self.enrichment_calls = 0

    async def publish_enrichment_run(self):
        self.enrichment_calls += 1
        return self.run_id


class FakeJobRun:
    def __init__(
        self,
        run_id,
        kind,
        status,
        error=None,
        result_summary=None,
        updated_at=None,
        lease_expires_at=None,
    ):
        self.run_id = run_id
        self.kind = kind
        self.status = status
        self.error = error
        self.result_summary = result_summary
        self.updated_at = updated_at or datetime.now(timezone.utc)
        self.lease_expires_at = lease_expires_at


class FakeJobRunsRepository:
    def __init__(self, runs=None):
        self.runs: dict[str, FakeJobRun] = {run.run_id: run for run in (runs or [])}
        self.marked_failed: list[tuple[str, str]] = []

    async def get(self, run_id):
        return self.runs.get(run_id)

    async def get_latest_by_kind(self, kind):
        matching = [run for run in self.runs.values() if run.kind == kind]
        return matching[-1] if matching else None

    async def find_active_global_run(self, kind):
        matching = [run for run in self.runs.values() if run.kind == kind and run.status not in TERMINAL_STATUSES]
        return matching[-1] if matching else None

    async def mark_failed(self, run_id, error):
        self.marked_failed.append((run_id, error))
        run = self.runs.get(run_id)
        if run is not None:
            run.status = JOB_STATUS_FAILED
            run.error = error

    async def cancel(self, run_id, reason):
        run = self.runs.get(run_id)
        if run is None or run.status in TERMINAL_STATUSES:
            return False
        run.status = JOB_STATUS_CANCELLED
        run.error = reason
        return True


def _build(job_runs_repository=None):
    repository = FakeRepository()
    token_crypto = TokenCrypto(TokenCrypto.generate_key())
    validator = FakeTokenValidator()
    publisher = FakePublisher()
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=validator,
    )
    app.state.queue_publisher = publisher
    app.state.job_runs_repository = job_runs_repository or FakeJobRunsRepository()
    return TestClient(app), validator, publisher


def _admin_token(validator: FakeTokenValidator) -> str:
    token = new_opaque_token()
    validator.register(token, _claims(is_admin=True))
    return token


def _non_admin_token(validator: FakeTokenValidator) -> str:
    token = new_opaque_token()
    validator.register(token, _claims(is_admin=False))
    return token


def _status_path(client: TestClient, run_id: str) -> str:
    return _path(client, enrichment_routes.get_enrichment_run_status, run_id=run_id)


def _cancel_path(client: TestClient, run_id: str) -> str:
    return _path(client, enrichment_routes.cancel_enrichment_run, run_id=run_id)


def test_requires_bearer_token():
    client, _validator, _publisher = _build()

    response = client.post(_path(client, enrichment_routes.start_enrichment_run))

    assert response.status_code == 401


def test_non_admin_scope_is_forbidden():
    client, validator, publisher = _build()
    token = _non_admin_token(validator)

    response = client.post(_path(client, enrichment_routes.start_enrichment_run), headers=_bearer(token))

    assert response.status_code == 403
    assert publisher.enrichment_calls == 0


def test_admin_scope_publishes_and_returns_run_id():
    client, validator, publisher = _build()
    token = _admin_token(validator)

    response = client.post(_path(client, enrichment_routes.start_enrichment_run), headers=_bearer(token))

    assert response.status_code == 202
    assert EnrichmentRunResponse.model_validate(response.json()) == EnrichmentRunResponse(run_id=publisher.run_id)
    assert publisher.enrichment_calls == 1


def test_a_live_in_flight_run_is_returned_rather_than_queueing_a_second_one():
    in_flight_run_id = new_run_id()
    running = FakeJobRun(
        in_flight_run_id,
        JOB_KIND_ENRICHMENT,
        JOB_STATUS_RUNNING,
        lease_expires_at=datetime.now(timezone.utc) + new_short_interval(),
    )
    job_runs = FakeJobRunsRepository([running])
    client, validator, publisher = _build(job_runs)
    token = _admin_token(validator)

    response = client.post(_path(client, enrichment_routes.start_enrichment_run), headers=_bearer(token))

    assert response.status_code == 202
    assert EnrichmentRunResponse.model_validate(response.json()) == EnrichmentRunResponse(run_id=in_flight_run_id)
    assert publisher.enrichment_calls == 0
    assert job_runs.marked_failed == []


def test_a_running_run_whose_lease_lapsed_is_superseded_by_a_fresh_one():
    lapsed_run_id = new_run_id()
    lapsed = FakeJobRun(
        lapsed_run_id,
        JOB_KIND_ENRICHMENT,
        JOB_STATUS_RUNNING,
        lease_expires_at=datetime.now(timezone.utc) - new_short_interval(),
    )
    job_runs = FakeJobRunsRepository([lapsed])
    client, validator, publisher = _build(job_runs)
    token = _admin_token(validator)

    response = client.post(_path(client, enrichment_routes.start_enrichment_run), headers=_bearer(token))

    assert response.status_code == 202
    assert EnrichmentRunResponse.model_validate(response.json()) == EnrichmentRunResponse(run_id=publisher.run_id)
    assert publisher.enrichment_calls == 1
    assert job_runs.marked_failed == [(lapsed_run_id, lease_lapsed_reason(ENRICHMENT_RUN_NOUN))]


def test_a_queued_run_that_made_no_progress_for_a_day_is_superseded():
    stuck_run_id = new_run_id()
    stuck = FakeJobRun(
        stuck_run_id,
        JOB_KIND_ENRICHMENT,
        _QUEUED_STATUS,
        updated_at=datetime.now(timezone.utc) - STALE_RUN_THRESHOLD - new_short_interval(),
    )
    job_runs = FakeJobRunsRepository([stuck])
    client, validator, publisher = _build(job_runs)
    token = _admin_token(validator)

    response = client.post(_path(client, enrichment_routes.start_enrichment_run), headers=_bearer(token))

    assert response.status_code == 202
    assert publisher.enrichment_calls == 1
    assert job_runs.marked_failed == [(stuck_run_id, no_progress_reason(ENRICHMENT_RUN_NOUN))]


def test_a_terminal_run_never_blocks_a_new_one():
    finished = FakeJobRun(new_run_id(), JOB_KIND_ENRICHMENT, JOB_STATUS_SUCCEEDED)
    job_runs = FakeJobRunsRepository([finished])
    client, validator, publisher = _build(job_runs)
    token = _admin_token(validator)

    response = client.post(_path(client, enrichment_routes.start_enrichment_run), headers=_bearer(token))

    assert response.status_code == 202
    assert EnrichmentRunResponse.model_validate(response.json()) == EnrichmentRunResponse(run_id=publisher.run_id)
    assert publisher.enrichment_calls == 1


def test_queue_not_configured_returns_503():
    client, validator, _publisher = _build()
    client.app.state.queue_publisher = None
    token = _admin_token(validator)

    response = client.post(_path(client, enrichment_routes.start_enrichment_run), headers=_bearer(token))

    assert response.status_code == 503


def test_get_latest_run_requires_bearer_token():
    client, _validator, _publisher = _build()

    response = client.get(_path(client, enrichment_routes.get_latest_enrichment_run))

    assert response.status_code == 401


def test_get_latest_run_non_admin_scope_is_forbidden():
    client, validator, _publisher = _build()
    token = _non_admin_token(validator)

    response = client.get(_path(client, enrichment_routes.get_latest_enrichment_run), headers=_bearer(token))

    assert response.status_code == 403


def test_get_latest_run_returns_the_most_recent_run_of_that_kind():
    run_id = new_run_id()
    result_summary = new_result_summary()
    run = FakeJobRun(run_id, JOB_KIND_ENRICHMENT, JOB_STATUS_SUCCEEDED, result_summary=result_summary)
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    token = _admin_token(validator)

    response = client.get(_path(client, enrichment_routes.get_latest_enrichment_run), headers=_bearer(token))

    assert response.status_code == 200
    assert EnrichmentRunStatusResponse.model_validate(response.json()) == EnrichmentRunStatusResponse(
        run_id=run_id, status=JOB_STATUS_SUCCEEDED, error=None, result_summary=result_summary
    )


def test_get_latest_run_404_when_no_run_ever_queued():
    client, validator, _publisher = _build()
    token = _admin_token(validator)

    response = client.get(_path(client, enrichment_routes.get_latest_enrichment_run), headers=_bearer(token))

    assert response.status_code == 404


def test_get_latest_run_route_is_not_captured_by_the_run_id_route():
    """/runs/latest must resolve to the dedicated route, not get captured as run_id='latest' by
    /runs/{run_id} -- this only holds if /runs/latest is registered first in enrichment_routes.py."""
    run_id = new_run_id()
    run = FakeJobRun(run_id, JOB_KIND_ENRICHMENT, JOB_STATUS_SUCCEEDED)
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    token = _admin_token(validator)

    response = client.get(_path(client, enrichment_routes.get_latest_enrichment_run), headers=_bearer(token))

    assert response.status_code == 200
    assert EnrichmentRunStatusResponse.model_validate(response.json()).run_id == run_id


def test_get_run_status_requires_bearer_token():
    client, _validator, _publisher = _build()

    response = client.get(_path(client, enrichment_routes.get_enrichment_run_status, run_id=new_run_id()))

    assert response.status_code == 401


def test_get_run_status_non_admin_scope_is_forbidden():
    run_id = new_run_id()
    client, validator, _publisher = _build(
        FakeJobRunsRepository([FakeJobRun(run_id, JOB_KIND_ENRICHMENT, JOB_STATUS_RUNNING)])
    )
    token = _non_admin_token(validator)

    response = client.get(_status_path(client, run_id), headers=_bearer(token))

    assert response.status_code == 403


def test_get_run_status_returns_the_run():
    run_id = new_run_id()
    error = lowercase_token()
    run = FakeJobRun(run_id, JOB_KIND_ENRICHMENT, JOB_STATUS_FAILED, error=error)
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    token = _admin_token(validator)

    response = client.get(_status_path(client, run_id), headers=_bearer(token))

    assert response.status_code == 200
    assert EnrichmentRunStatusResponse.model_validate(response.json()) == EnrichmentRunStatusResponse(
        run_id=run_id, status=JOB_STATUS_FAILED, error=error, result_summary=None
    )


def test_get_run_status_404_when_unknown_run_id():
    client, validator, _publisher = _build()
    token = _admin_token(validator)

    response = client.get(
        _path(client, enrichment_routes.get_enrichment_run_status, run_id=new_run_id()), headers=_bearer(token)
    )

    assert response.status_code == 404


def test_get_run_status_404_when_run_is_not_an_enrichment_kind():
    run_id = new_run_id()
    run = FakeJobRun(run_id, JOB_KIND_LIBRARY_REFRESH, JOB_STATUS_SUCCEEDED)
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    token = _admin_token(validator)

    response = client.get(_status_path(client, run_id), headers=_bearer(token))

    assert response.status_code == 404


def test_cancel_requires_bearer_token():
    client, _validator, _publisher = _build()

    response = client.post(_path(client, enrichment_routes.cancel_enrichment_run, run_id=new_run_id()))

    assert response.status_code == 401


def test_cancel_non_admin_scope_is_forbidden():
    run_id = new_run_id()
    job_runs = FakeJobRunsRepository([FakeJobRun(run_id, JOB_KIND_ENRICHMENT, _QUEUED_STATUS)])
    client, validator, _publisher = _build(job_runs)
    token = _non_admin_token(validator)

    response = client.post(_cancel_path(client, run_id), headers=_bearer(token))

    assert response.status_code == 403
    assert job_runs.runs[run_id].status == _QUEUED_STATUS


def test_cancel_stands_a_stuck_queued_run_down_and_returns_it():
    run_id = new_run_id()
    job_runs = FakeJobRunsRepository([FakeJobRun(run_id, JOB_KIND_ENRICHMENT, _QUEUED_STATUS)])
    client, validator, _publisher = _build(job_runs)
    token = _admin_token(validator)

    response = client.post(_cancel_path(client, run_id), headers=_bearer(token))

    assert response.status_code == 200
    assert EnrichmentRunStatusResponse.model_validate(response.json()) == EnrichmentRunStatusResponse(
        run_id=run_id, status=JOB_STATUS_CANCELLED, error=CANCELLED_BY_ADMIN, result_summary=None
    )


def test_a_cancelled_run_no_longer_blocks_a_fresh_one():
    """The whole point of cancelling: POST /enrichment/runs returned the stuck run's id until 24h of
    staleness elapsed, so an operator had no remedy at all."""
    stuck_run_id = new_run_id()
    job_runs = FakeJobRunsRepository([FakeJobRun(stuck_run_id, JOB_KIND_ENRICHMENT, _QUEUED_STATUS)])
    client, validator, publisher = _build(job_runs)
    token = _admin_token(validator)

    client.post(_path(client, enrichment_routes.cancel_enrichment_run, run_id=stuck_run_id), headers=_bearer(token))
    response = client.post(_path(client, enrichment_routes.start_enrichment_run), headers=_bearer(token))

    assert EnrichmentRunResponse.model_validate(response.json()) == EnrichmentRunResponse(run_id=publisher.run_id)
    assert publisher.enrichment_calls == 1


def test_cancel_409_when_the_run_has_already_finished():
    run_id = new_run_id()
    job_runs = FakeJobRunsRepository([FakeJobRun(run_id, JOB_KIND_ENRICHMENT, JOB_STATUS_SUCCEEDED)])
    client, validator, _publisher = _build(job_runs)
    token = _admin_token(validator)

    response = client.post(_cancel_path(client, run_id), headers=_bearer(token))

    assert response.status_code == 409
    assert job_runs.runs[run_id].status == JOB_STATUS_SUCCEEDED, "a late cancel must not rewrite a finished outcome"


def test_cancel_404_when_run_is_not_an_enrichment_kind():
    run_id = new_run_id()
    job_runs = FakeJobRunsRepository([FakeJobRun(run_id, JOB_KIND_LIBRARY_REFRESH, _QUEUED_STATUS)])
    client, validator, _publisher = _build(job_runs)
    token = _admin_token(validator)

    response = client.post(_cancel_path(client, run_id), headers=_bearer(token))

    assert response.status_code == 404
    assert job_runs.runs[run_id].status == _QUEUED_STATUS, (
        "a library refresh is ownership-checked, so it must not be cancellable through the admin route"
    )


def test_cancel_404_when_unknown_run_id():
    client, validator, _publisher = _build()
    token = _admin_token(validator)

    response = client.post(
        _path(client, enrichment_routes.cancel_enrichment_run, run_id=new_run_id()), headers=_bearer(token)
    )

    assert response.status_code == 404
