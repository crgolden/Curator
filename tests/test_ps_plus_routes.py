"""Tests for GET /me/ps-plus-rotation and its summary, using create_app() with a fake PsPlusRepository."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from curator import ps_plus_routes
from curator.app import create_app
from curator.catalog.ps_plus_repository import (
    PsPlusCategoryState,
    PsPlusRotationReport,
    PsPlusRotationSummary,
    PsPlusTitle,
)
from curator.deps import PREFERENCE_NOT_LINKED_DETAIL
from curator.persistence.crypto import TokenCrypto
from curator.ps_plus_routes import (
    PsPlusCategoryResponse,
    PsPlusRotationResponse,
    PsPlusRotationSummaryResponse,
    PsPlusTitleResponse,
)
from curator.psn.title_platform import PS4
from test_routes import (
    FakeAgentFactory,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _path,
    _seed_link,
)
from test_values import (
    new_category_id,
    new_game_id,
    new_game_title,
    new_identity_sub,
    new_opaque_token,
    new_positive_count,
    new_ps4_title_id,
    new_ps_plus_tier,
    new_small_count,
    new_store_product_id,
    new_utc_instant,
)

TOKEN = new_opaque_token()

_WALKED_AT = new_utc_instant()


def _title() -> PsPlusTitle:
    return PsPlusTitle(
        title_id=new_ps4_title_id(),
        game_id=new_game_id(),
        title=new_game_title(),
        tier=new_ps_plus_tier(),
        platforms=(PS4,),
        cover_image_url=None,
        store_product_id=new_store_product_id(),
        since_at=_WALKED_AT,
    )


def _title_response(title: PsPlusTitle) -> PsPlusTitleResponse:
    return PsPlusTitleResponse(
        title_id=title.title_id,
        game_id=title.game_id,
        title=title.title,
        tier=title.tier,
        platforms=list(title.platforms),
        cover_image_url=title.cover_image_url,
        store_product_id=title.store_product_id,
        since_at=title.since_at,
    )


class FakePsPlusRepository:
    def __init__(self, report=None, summary=None, latest_walk_started_at=None):
        self._report = report
        self._summary = summary
        self._latest_walk_started_at = latest_walk_started_at or datetime.now(timezone.utc)
        self.report_calls: list[str] = []
        self.summary_calls: list[str] = []

    async def rotation_report(self, identity_sub):
        self.report_calls.append(identity_sub)
        return self._report

    async def rotation_summary(self, identity_sub):
        self.summary_calls.append(identity_sub)
        return self._summary

    async def latest_walk_started_at(self):
        return self._latest_walk_started_at


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
    validator.register(TOKEN, _claims(sub=sub))

    response = client.get(_path(client, ps_plus_routes.get_ps_plus_rotation), headers=_bearer(TOKEN))

    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL


def test_the_report_carries_every_list_and_the_category_walk_state():
    sub = new_identity_sub()
    unclaimed = _title()
    lapsed = replace(_title(), tier=None, game_id=None)
    category = PsPlusCategoryState(
        category_id=new_category_id(),
        tier=new_ps_plus_tier(),
        walked_at=_WALKED_AT,
        total=new_positive_count(),
        previous_completed_at=None,
    )
    report = PsPlusRotationReport(
        catalog_walked_at=_WALKED_AT,
        since=None,
        added=[],
        leaving=[],
        unclaimed=[unclaimed],
        lapsed=[lapsed],
        categories=[category],
    )
    fake = FakePsPlusRepository(report=report)
    client, validator = _build(fake, linked_sub=sub)
    validator.register(TOKEN, _claims(sub=sub))

    response = client.get(_path(client, ps_plus_routes.get_ps_plus_rotation), headers=_bearer(TOKEN))

    assert response.status_code == 200
    assert PsPlusRotationResponse.model_validate(response.json()) == PsPlusRotationResponse(
        catalog_walked_at=_WALKED_AT,
        since=None,
        added=[],
        leaving=[],
        unclaimed=[_title_response(unclaimed)],
        lapsed=[_title_response(lapsed)],
        categories=[PsPlusCategoryResponse(tier=category.tier, walked_at=category.walked_at, total=category.total)],
    )
    assert fake.report_calls == [sub]


def test_the_summary_carries_only_the_two_actionable_counts():
    sub = new_identity_sub()
    summary = PsPlusRotationSummary(
        catalog_walked_at=_WALKED_AT, unclaimed=new_small_count(), leaving=new_small_count()
    )
    fake = FakePsPlusRepository(summary=summary)
    client, validator = _build(fake, linked_sub=sub)
    validator.register(TOKEN, _claims(sub=sub))

    response = client.get(_path(client, ps_plus_routes.get_ps_plus_rotation_summary), headers=_bearer(TOKEN))

    assert response.status_code == 200
    assert PsPlusRotationSummaryResponse.model_validate(response.json()) == PsPlusRotationSummaryResponse(
        catalog_walked_at=summary.catalog_walked_at, unclaimed=summary.unclaimed, leaving=summary.leaving
    )
    assert fake.summary_calls == [sub]


def test_the_summary_needs_a_psn_link():
    sub = new_identity_sub()
    client, validator = _build(FakePsPlusRepository())
    validator.register(TOKEN, _claims(sub=sub))

    response = client.get(_path(client, ps_plus_routes.get_ps_plus_rotation_summary), headers=_bearer(TOKEN))

    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL


def test_the_scheduler_is_constructed():
    client, _validator = _build(FakePsPlusRepository())

    scheduler = client.app.state.ps_plus_walk_scheduler

    assert scheduler is not None


def test_the_lifespan_starts_the_scheduler_and_stops_it_on_shutdown():
    client, _validator = _build(FakePsPlusRepository())
    scheduler = client.app.state.ps_plus_walk_scheduler

    assert scheduler._task is None, "nothing may start before the lifespan runs"

    with client:
        started = scheduler._task

    assert started is not None, (
        "the weekly walk only happens if the lifespan starts the scheduler; "
        "constructing it and publishing it on app.state does nothing on its own"
    )
    assert scheduler._task is None, "shutdown must cancel the task rather than leave it running"
