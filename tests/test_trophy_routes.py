"""Tests for GET /trophies/* -- create_app wired with a hand-written fake trophy_client_factory (the same
DI-seam style as test_routes.py's FakeAgentFactory), so no real PSN/Redis calls happen.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from audit_fakes import RecordingAuditRepository
from curator import trophy_routes
from curator.app import create_app
from curator.audit.repository import ACTION_TROPHY_FETCH, OUTCOME_COMPLETED, OUTCOME_FAILED
from curator.deps import HARVEST_TROPHIES, PREFERENCE_NOT_LINKED_DETAIL, preference_disabled_detail
from curator.persistence.crypto import TokenCrypto
from curator.psn.errors import PsnAuthError
from curator.psn.models import (
    ALL_TROPHY_GROUPS,
    TitleStat,
    TrophyCounts,
    TrophyDetail,
    TrophyGroups,
    TrophySummary,
    TrophyTitle,
)
from curator.psn.title_platform import PS5
from curator.trophy_routes import (
    GROUP_PARAM,
    LIMIT_PARAM,
    PLATFORM_PARAM,
    TitleTrophiesResponse,
    TrophyCountsResponse,
    TrophyDetailResponse,
    TrophyGroupsResponse,
    TrophySummaryResponse,
    TrophyTitleResponse,
    TrophyTitlesResponse,
)
from test_routes import FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _path, _seed_link
from test_values import (
    lowercase_token,
    new_account_id,
    new_email_address,
    new_flag,
    new_game_title,
    new_identity_sub,
    new_np_communication_id,
    new_opaque_token,
    new_percent_completed,
    new_positive_count,
    new_ps4_title_id,
    new_review_score,
    new_small_count,
    new_trophy_group_id,
    new_utc_instant,
)


def _new_counts() -> TrophyCounts:
    return TrophyCounts(
        bronze=new_positive_count(),
        silver=new_positive_count(),
        gold=new_positive_count(),
        platinum=new_small_count(),
    )


def _new_title() -> TrophyTitle:
    return TrophyTitle(
        name=new_game_title(),
        np_communication_id=new_np_communication_id(),
        platforms=(PS5,),
        progress=new_percent_completed(),
        earned=_new_counts(),
        defined=_new_counts(),
        last_updated=new_utc_instant().isoformat(),
    )


class FakeTrophyClient:
    """Stands in for TrophyClient/CachedTrophyClient: canned results, or raises PsnAuthError when armed."""

    def __init__(self, *, raise_auth_error=False, titles=None):
        self.raise_auth_error = raise_auth_error
        self.summary = TrophySummary(
            level=new_positive_count(),
            progress=new_percent_completed(),
            tier=new_small_count(),
            earned=_new_counts(),
            account_id=new_account_id(),
        )
        self.titles = titles if titles is not None else [_new_title()]
        self.trophy = TrophyDetail(
            trophy_id=new_positive_count(),
            name=new_game_title(),
            detail=lowercase_token(),
            type=lowercase_token(),
            hidden=new_flag(),
            earned=new_flag(),
            rarity=new_review_score(),
        )
        self.groups = TrophyGroups(
            title_name=new_game_title(),
            platforms=(PS5,),
            progress=new_percent_completed(),
            defined=_new_counts(),
            earned=_new_counts(),
            groups=(),
        )
        self.title_trophies_calls = []
        self.trophy_groups_calls = []

    def _raise_when_armed(self):
        if self.raise_auth_error:
            raise PsnAuthError(lowercase_token())

    async def trophy_summary(self, online_id=None, account_id=None):
        self._raise_when_armed()
        return self.summary

    async def trophy_titles(self, online_id=None, account_id=None, limit=100):
        self._raise_when_armed()
        return self.titles

    async def title_trophies(
        self, np_communication_id, platform, online_id=None, account_id=None, group=ALL_TROPHY_GROUPS, limit=None
    ):
        self._raise_when_armed()
        self.title_trophies_calls.append((np_communication_id, platform, group))
        return [self.trophy]

    async def trophy_groups(self, np_communication_id, platform, online_id=None, account_id=None):
        self._raise_when_armed()
        self.trophy_groups_calls.append((np_communication_id, platform))
        return self.groups

    async def title_stats(self, online_id=None, account_id=None, limit=200):
        self._raise_when_armed()
        return [TitleStat(title_id=new_ps4_title_id(), name=new_game_title(), play_count=new_small_count())]


class FakeTrophyClientFactory:
    """Records every ``sub`` requested; raises ``RuntimeError`` for any ``sub`` not explicitly linked."""

    def __init__(self):
        self.linked: dict[str, FakeTrophyClient] = {}
        self.calls: list[str] = []

    async def __call__(self, sub):
        self.calls.append(sub)
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(sub)
        return client


class _Caller:
    def __init__(self) -> None:
        self.sub = new_identity_sub()
        self.token = new_opaque_token()


def _build(caller: _Caller, trophy_client_factory=None, repository=None):
    settings = _make_settings()
    repository = repository if repository is not None else FakeRepository()
    validator = FakeTokenValidator()
    validator.register(caller.token, _claims(sub=caller.sub, email=new_email_address()))
    app = create_app(
        settings,
        repository=repository,
        token_validator=validator,
        trophy_client_factory=trophy_client_factory or FakeTrophyClientFactory(),
        audit_repository=RecordingAuditRepository(),
    )
    return TestClient(app), app.state.trophy_client_factory


def _build_linked(caller: _Caller, trophy_client_factory=None):
    """Build an app whose caller has a PSN link with ``harvest_trophies`` enabled."""
    repository = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    _seed_link(repository, crypto, caller.sub, harvest_trophies=True)
    return _build(caller, trophy_client_factory, repository=repository)


def _build_with_trophies_disabled(caller: _Caller):
    repository = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    _seed_link(repository, crypto, caller.sub, harvest_trophies=False)
    return _build(caller, repository=repository)


def _linked_factory(caller: _Caller, fake_client: FakeTrophyClient) -> FakeTrophyClientFactory:
    factory = FakeTrophyClientFactory()
    factory.linked[caller.sub] = fake_client
    return factory


def _counts_response(counts: TrophyCounts) -> TrophyCountsResponse:
    return TrophyCountsResponse(bronze=counts.bronze, silver=counts.silver, gold=counts.gold, platinum=counts.platinum)


def _summary_path(client: TestClient) -> str:
    return _path(client, trophy_routes.get_trophy_summary)


def _title_trophies_path(client: TestClient, np_communication_id: str) -> str:
    return _path(client, trophy_routes.get_title_trophies, np_communication_id=np_communication_id)


def _groups_path(client: TestClient, np_communication_id: str) -> str:
    return _path(client, trophy_routes.get_trophy_groups, np_communication_id=np_communication_id)


def test_trophy_summary_no_link_is_404():
    caller = _Caller()
    client, _ = _build(caller)

    response = client.get(_summary_path(client), headers=_bearer(caller.token))

    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL


def test_trophy_summary_happy_path():
    caller = _Caller()
    fake_client = FakeTrophyClient()
    factory = _linked_factory(caller, fake_client)
    client, _ = _build_linked(caller, factory)

    response = client.get(_summary_path(client), headers=_bearer(caller.token))

    assert response.status_code == 200
    summary = fake_client.summary
    assert TrophySummaryResponse.model_validate(response.json()) == TrophySummaryResponse(
        level=summary.level,
        progress=summary.progress,
        tier=summary.tier,
        earned=_counts_response(summary.earned),
        account_id=summary.account_id,
    )
    assert factory.calls == [caller.sub]
    assert client.app.state.audit_repository.outcomes == [(ACTION_TROPHY_FETCH, OUTCOME_COMPLETED)]


def test_trophy_summary_psn_auth_error_is_401():
    caller = _Caller()
    factory = _linked_factory(caller, FakeTrophyClient(raise_auth_error=True))
    client, _ = _build_linked(caller, factory)

    response = client.get(_summary_path(client), headers=_bearer(caller.token))

    assert response.status_code == 401
    assert client.app.state.audit_repository.outcomes == [(ACTION_TROPHY_FETCH, OUTCOME_FAILED)]


def test_trophy_summary_never_uses_the_token_when_the_history_row_cannot_be_written():
    caller = _Caller()
    factory = _linked_factory(caller, FakeTrophyClient())
    client, _ = _build_linked(caller, factory)
    client.app.state.audit_repository.begin_error = RuntimeError(caller.sub)

    with pytest.raises(RuntimeError):
        client.get(_summary_path(client), headers=_bearer(caller.token))

    assert factory.calls == []


def test_trophy_summary_harvest_trophies_disabled_is_403():
    caller = _Caller()
    client, _ = _build_with_trophies_disabled(caller)

    response = client.get(_summary_path(client), headers=_bearer(caller.token))

    assert response.status_code == 403
    assert response.json()["detail"] == preference_disabled_detail(HARVEST_TROPHIES)


def test_trophy_titles_happy_path():
    caller = _Caller()
    fake_client = FakeTrophyClient()
    client, _ = _build_linked(caller, _linked_factory(caller, fake_client))
    (title,) = fake_client.titles

    response = client.get(
        _path(client, trophy_routes.get_trophy_titles),
        params={LIMIT_PARAM: new_positive_count()},
        headers=_bearer(caller.token),
    )

    assert response.status_code == 200
    assert TrophyTitlesResponse.model_validate(response.json()) == TrophyTitlesResponse(
        titles=[
            TrophyTitleResponse(
                name=title.name,
                np_communication_id=title.np_communication_id,
                platforms=list(title.platforms),
                progress=title.progress,
                earned=_counts_response(title.earned),
                defined=_counts_response(title.defined),
                last_updated=title.last_updated,
            )
        ]
    )


def test_trophy_titles_harvest_trophies_disabled_is_403():
    caller = _Caller()
    client, _ = _build_with_trophies_disabled(caller)

    response = client.get(_path(client, trophy_routes.get_trophy_titles), headers=_bearer(caller.token))

    assert response.status_code == 403
    assert response.json()["detail"] == preference_disabled_detail(HARVEST_TROPHIES)


def test_title_trophies_requires_platform_query_param():
    caller = _Caller()
    client, _ = _build_linked(caller, _linked_factory(caller, FakeTrophyClient()))

    response = client.get(_title_trophies_path(client, new_np_communication_id()), headers=_bearer(caller.token))

    assert response.status_code == 422


def test_title_trophies_happy_path():
    caller = _Caller()
    fake_client = FakeTrophyClient()
    client, _ = _build_linked(caller, _linked_factory(caller, fake_client))
    np_communication_id = new_np_communication_id()
    group = new_trophy_group_id()

    response = client.get(
        _title_trophies_path(client, np_communication_id),
        params={PLATFORM_PARAM: PS5, GROUP_PARAM: group},
        headers=_bearer(caller.token),
    )

    assert response.status_code == 200
    trophy = fake_client.trophy
    assert TitleTrophiesResponse.model_validate(response.json()) == TitleTrophiesResponse(
        trophies=[
            TrophyDetailResponse(
                trophy_id=trophy.trophy_id,
                name=trophy.name,
                detail=trophy.detail,
                type=trophy.type,
                hidden=trophy.hidden,
                icon_url=None,
                earned=trophy.earned,
                earned_date=None,
                progress_rate=None,
                rarity=trophy.rarity,
            )
        ]
    )
    assert fake_client.title_trophies_calls == [(np_communication_id, PS5, group)]


def test_title_trophies_harvest_trophies_disabled_is_403():
    caller = _Caller()
    client, _ = _build_with_trophies_disabled(caller)

    response = client.get(
        _title_trophies_path(client, new_np_communication_id()),
        params={PLATFORM_PARAM: PS5},
        headers=_bearer(caller.token),
    )

    assert response.status_code == 403
    assert response.json()["detail"] == preference_disabled_detail(HARVEST_TROPHIES)


def test_trophy_groups_happy_path():
    caller = _Caller()
    fake_client = FakeTrophyClient()
    client, _ = _build_linked(caller, _linked_factory(caller, fake_client))
    np_communication_id = new_np_communication_id()

    response = client.get(
        _groups_path(client, np_communication_id), params={PLATFORM_PARAM: PS5}, headers=_bearer(caller.token)
    )

    assert response.status_code == 200
    groups = fake_client.groups
    assert TrophyGroupsResponse.model_validate(response.json()) == TrophyGroupsResponse(
        title_name=groups.title_name,
        platforms=list(groups.platforms),
        progress=groups.progress,
        defined=_counts_response(groups.defined),
        earned=_counts_response(groups.earned),
        groups=[],
        last_updated=None,
    )
    assert fake_client.trophy_groups_calls == [(np_communication_id, PS5)]


def test_trophy_groups_no_link_is_404():
    caller = _Caller()
    client, _ = _build(caller)

    response = client.get(
        _groups_path(client, new_np_communication_id()), params={PLATFORM_PARAM: PS5}, headers=_bearer(caller.token)
    )

    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL


def test_trophy_groups_harvest_trophies_disabled_is_403():
    caller = _Caller()
    client, _ = _build_with_trophies_disabled(caller)

    response = client.get(
        _groups_path(client, new_np_communication_id()), params={PLATFORM_PARAM: PS5}, headers=_bearer(caller.token)
    )

    assert response.status_code == 403
    assert response.json()["detail"] == preference_disabled_detail(HARVEST_TROPHIES)
