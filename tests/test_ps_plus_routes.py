"""Tests for GET /me/ps-plus-rotation and its summary, using create_app() with a fake PsPlusRepository."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from curator.app import create_app
from curator.catalog.ps_plus_repository import (
    PsPlusCategoryState,
    PsPlusRotationReport,
    PsPlusRotationSummary,
    PsPlusTitle,
)
from curator.persistence.crypto import TokenCrypto
from test_routes import (
    FakeAgentFactory,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _seed_link,
)
from test_values import new_game_id, new_game_title, new_identity_sub, new_ps4_title_id, new_store_product_id

_WALKED_AT = datetime(2026, 9, 8, tzinfo=timezone.utc)


def _title(**overrides):
    fields = {
        "title_id": new_ps4_title_id(),
        "game_id": new_game_id(),
        "title": new_game_title(),
        "tier": "extra",
        "platforms": ("PS4",),
        "cover_image_url": None,
        "store_product_id": new_store_product_id(),
        "since_at": _WALKED_AT,
    }
    fields.update(overrides)
    return PsPlusTitle(**fields)


class FakePsPlusRepository:
    def __init__(self, report=None, summary=None):
        self._report = report
        self._summary = summary
        self.report_calls: list[str] = []
        self.summary_calls: list[str] = []

    async def rotation_report(self, identity_sub):
        self.report_calls.append(identity_sub)
        return self._report

    async def rotation_summary(self, identity_sub):
        self.summary_calls.append(identity_sub)
        return self._summary


def _build(ps_plus_repository, *, linked_sub=None):
    repository = FakeRepository()
    token_crypto = TokenCrypto(TokenCrypto.generate_key())
    validator = FakeTokenValidator()
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=validator,
        ps_plus_repository=ps_plus_repository,
    )
    if linked_sub is not None:
        _seed_link(repository, token_crypto, linked_sub)
    return TestClient(app), validator


def test_the_report_needs_a_psn_link():
    sub = new_identity_sub()
    client, validator = _build(FakePsPlusRepository())
    validator.register("token", _claims(sub=sub))

    response = client.get("/me/ps-plus-rotation", headers=_bearer("token"))

    assert response.status_code == 404


def test_the_report_carries_every_list_and_the_category_walk_state():
    sub = new_identity_sub()
    unclaimed = _title()
    lapsed = _title(tier=None, game_id=None)
    report = PsPlusRotationReport(
        catalog_walked_at=_WALKED_AT,
        since=None,
        added=[],
        leaving=[],
        unclaimed=[unclaimed],
        lapsed=[lapsed],
        categories=[
            PsPlusCategoryState(
                category_id="cat", tier="extra", walked_at=_WALKED_AT, total=489, previous_completed_at=None
            )
        ],
    )
    fake = FakePsPlusRepository(report=report)
    client, validator = _build(fake, linked_sub=sub)
    validator.register("token", _claims(sub=sub))

    response = client.get("/me/ps-plus-rotation", headers=_bearer("token"))

    assert response.status_code == 200
    body = response.json()
    assert body["since"] is None
    assert body["added"] == []
    assert [entry["title_id"] for entry in body["unclaimed"]] == [unclaimed.title_id]
    assert body["unclaimed"][0]["game_id"] == unclaimed.game_id
    assert body["lapsed"][0]["tier"] is None
    assert body["categories"] == [{"tier": "extra", "walked_at": "2026-09-08T00:00:00Z", "total": 489}]
    assert fake.report_calls == [sub]


def test_the_summary_carries_only_the_two_actionable_counts():
    sub = new_identity_sub()
    fake = FakePsPlusRepository(summary=PsPlusRotationSummary(catalog_walked_at=_WALKED_AT, unclaimed=7, leaving=2))
    client, validator = _build(fake, linked_sub=sub)
    validator.register("token", _claims(sub=sub))

    response = client.get("/me/ps-plus-rotation/summary", headers=_bearer("token"))

    assert response.status_code == 200
    assert response.json() == {"catalog_walked_at": "2026-09-08T00:00:00Z", "unclaimed": 7, "leaving": 2}
    assert fake.summary_calls == [sub]


def test_the_summary_needs_a_psn_link():
    sub = new_identity_sub()
    client, validator = _build(FakePsPlusRepository())
    validator.register("token", _claims(sub=sub))

    assert client.get("/me/ps-plus-rotation/summary", headers=_bearer("token")).status_code == 404


def test_the_scheduler_is_constructed_but_not_started():
    client, _validator = _build(FakePsPlusRepository())

    scheduler = client.app.state.ps_plus_walk_scheduler

    assert scheduler is not None
    assert scheduler._task is None
