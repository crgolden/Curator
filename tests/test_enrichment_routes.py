"""Tests for POST /enrichment/runs, admin-scoped, using create_app() with a fake QueuePublisher."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings


class FakePublisher:
    def __init__(self, run_id="run-1"):
        self._run_id = run_id
        self.enrichment_calls = 0

    async def publish_enrichment_run(self):
        self.enrichment_calls += 1
        return self._run_id


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
        matching = [run for run in self.runs.values() if run.kind == kind and run.status not in ("succeeded", "failed")]
        return matching[-1] if matching else None

    async def mark_failed(self, run_id, error):
        self.marked_failed.append((run_id, error))
        run = self.runs.get(run_id)
        if run is not None:
            run.status = "failed"
            run.error = error


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


def test_requires_bearer_token():
    client, _validator, _publisher = _build()

    response = client.post("/enrichment/runs")

    assert response.status_code == 401


def test_non_admin_scope_is_forbidden():
    client, validator, publisher = _build()
    validator.register("token-a", _claims(is_admin=False))

    response = client.post("/enrichment/runs", headers=_bearer("token-a"))

    assert response.status_code == 403
    assert publisher.enrichment_calls == 0


def test_admin_scope_publishes_and_returns_run_id():
    client, validator, publisher = _build()
    validator.register("token-a", _claims(is_admin=True))

    response = client.post("/enrichment/runs", headers=_bearer("token-a"))

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1"}
    assert publisher.enrichment_calls == 1


def test_a_live_in_flight_run_is_returned_rather_than_queueing_a_second_one():
    running = FakeJobRun(
        "run-in-flight",
        "enrichment",
        "running",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    job_runs = FakeJobRunsRepository([running])
    client, validator, publisher = _build(job_runs)
    validator.register("token-a", _claims(is_admin=True))

    response = client.post("/enrichment/runs", headers=_bearer("token-a"))

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-in-flight"}
    assert publisher.enrichment_calls == 0
    assert job_runs.marked_failed == []


def test_a_running_run_whose_lease_lapsed_is_superseded_by_a_fresh_one():
    lapsed = FakeJobRun(
        "run-lapsed",
        "enrichment",
        "running",
        lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    job_runs = FakeJobRunsRepository([lapsed])
    client, validator, publisher = _build(job_runs)
    validator.register("token-a", _claims(is_admin=True))

    response = client.post("/enrichment/runs", headers=_bearer("token-a"))

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1"}
    assert publisher.enrichment_calls == 1
    assert [run_id for run_id, _reason in job_runs.marked_failed] == ["run-lapsed"]


def test_a_queued_run_that_made_no_progress_for_a_day_is_superseded():
    stuck = FakeJobRun(
        "run-stuck",
        "enrichment",
        "queued",
        updated_at=datetime.now(timezone.utc) - timedelta(hours=25),
    )
    job_runs = FakeJobRunsRepository([stuck])
    client, validator, publisher = _build(job_runs)
    validator.register("token-a", _claims(is_admin=True))

    response = client.post("/enrichment/runs", headers=_bearer("token-a"))

    assert response.status_code == 202
    assert publisher.enrichment_calls == 1
    assert [run_id for run_id, _reason in job_runs.marked_failed] == ["run-stuck"]


def test_a_terminal_run_never_blocks_a_new_one():
    finished = FakeJobRun("run-done", "enrichment", "succeeded")
    job_runs = FakeJobRunsRepository([finished])
    client, validator, publisher = _build(job_runs)
    validator.register("token-a", _claims(is_admin=True))

    response = client.post("/enrichment/runs", headers=_bearer("token-a"))

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1"}
    assert publisher.enrichment_calls == 1


def test_queue_not_configured_returns_503():
    client, validator, _publisher = _build()
    client.app.state.queue_publisher = None
    validator.register("token-a", _claims(is_admin=True))

    response = client.post("/enrichment/runs", headers=_bearer("token-a"))

    assert response.status_code == 503


def test_get_latest_run_requires_bearer_token():
    client, _validator, _publisher = _build()

    response = client.get("/enrichment/runs/latest")

    assert response.status_code == 401


def test_get_latest_run_non_admin_scope_is_forbidden():
    client, validator, _publisher = _build()
    validator.register("token-a", _claims(is_admin=False))

    response = client.get("/enrichment/runs/latest", headers=_bearer("token-a"))

    assert response.status_code == 403


def test_get_latest_run_returns_the_most_recent_run_of_that_kind():
    run = FakeJobRun("run-1", "enrichment", "succeeded", result_summary={"opencritic": {"status": "ok"}})
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims(is_admin=True))

    response = client.get("/enrichment/runs/latest", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == {
        "run_id": "run-1",
        "status": "succeeded",
        "error": None,
        "result_summary": {"opencritic": {"status": "ok"}},
    }


def test_get_latest_run_404_when_no_run_ever_queued():
    client, validator, _publisher = _build()
    validator.register("token-a", _claims(is_admin=True))

    response = client.get("/enrichment/runs/latest", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_get_latest_run_route_is_not_captured_by_the_run_id_route():
    """/runs/latest must resolve to the dedicated route, not get captured as run_id='latest' by
    /runs/{run_id} -- this only holds if /runs/latest is registered first in enrichment_routes.py."""
    run = FakeJobRun("run-1", "enrichment", "succeeded")
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims(is_admin=True))

    response = client.get("/enrichment/runs/latest", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json()["run_id"] == "run-1"


def test_get_run_status_requires_bearer_token():
    client, _validator, _publisher = _build()

    response = client.get("/enrichment/runs/run-1")

    assert response.status_code == 401


def test_get_run_status_non_admin_scope_is_forbidden():
    client, validator, _publisher = _build(FakeJobRunsRepository([FakeJobRun("run-1", "enrichment", "running")]))
    validator.register("token-a", _claims(is_admin=False))

    response = client.get("/enrichment/runs/run-1", headers=_bearer("token-a"))

    assert response.status_code == 403


def test_get_run_status_returns_the_run():
    run = FakeJobRun("run-1", "enrichment", "failed", error="boom")
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims(is_admin=True))

    response = client.get("/enrichment/runs/run-1", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == {"run_id": "run-1", "status": "failed", "error": "boom", "result_summary": None}


def test_get_run_status_404_when_unknown_run_id():
    client, validator, _publisher = _build()
    validator.register("token-a", _claims(is_admin=True))

    response = client.get("/enrichment/runs/unknown", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_get_run_status_404_when_run_is_not_an_enrichment_kind():
    run = FakeJobRun("run-1", "library_refresh", "succeeded")
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims(is_admin=True))

    response = client.get("/enrichment/runs/run-1", headers=_bearer("token-a"))

    assert response.status_code == 404
