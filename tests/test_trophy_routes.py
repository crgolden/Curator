"""Tests for GET /trophies/* -- create_app wired with a hand-written fake trophy_client_factory (the same
DI-seam style as test_routes.py's FakeAgentFactory), so no real PSN/Redis calls happen.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from curator.psn.errors import PsnAuthError
from curator.psn.models import TitleStat, TrophyCounts, TrophyDetail, TrophyGroups, TrophySummary, TrophyTitle
from test_routes import EMAIL, SUB, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _seed_link


class FakeTrophyClient:
    """Stands in for TrophyClient/CachedTrophyClient: canned results, or raises PsnAuthError when armed."""

    def __init__(self, *, raise_auth_error=False):
        self.raise_auth_error = raise_auth_error
        self.title_trophies_calls = []
        self.trophy_groups_calls = []

    async def trophy_summary(self, online_id=None, account_id=None):
        if self.raise_auth_error:
            raise PsnAuthError("boom")
        return TrophySummary(level=42, progress=87, tier=3, earned=TrophyCounts(gold=2, platinum=1), account_id="123")

    async def trophy_titles(self, online_id=None, account_id=None, limit=100):
        if self.raise_auth_error:
            raise PsnAuthError("boom")
        return [
            TrophyTitle(
                name="Game A",
                np_communication_id="NPWR1",
                platforms=("PS5",),
                progress=50,
                earned=TrophyCounts(gold=1),
                defined=TrophyCounts(gold=2),
                last_updated="2026-01-01T00:00:00Z",
            )
        ]

    async def title_trophies(
        self, np_communication_id, platform, online_id=None, account_id=None, group="all", limit=None
    ):
        if self.raise_auth_error:
            raise PsnAuthError("boom")
        self.title_trophies_calls.append((np_communication_id, platform, group))
        return [
            TrophyDetail(
                trophy_id=1,
                name="First Blood",
                detail="Do the thing",
                type="bronze",
                hidden=False,
                earned=True,
                rarity=42.5,
            )
        ]

    async def trophy_groups(self, np_communication_id, platform, online_id=None, account_id=None):
        if self.raise_auth_error:
            raise PsnAuthError("boom")
        self.trophy_groups_calls.append((np_communication_id, platform))
        return TrophyGroups(
            title_name="Game A",
            platforms=("PS5",),
            progress=50,
            defined=TrophyCounts(gold=2),
            earned=TrophyCounts(gold=1),
            groups=(),
        )

    async def title_stats(self, online_id=None, account_id=None, limit=200):
        if self.raise_auth_error:
            raise PsnAuthError("boom")
        return [TitleStat(title_id="CUSA00419_00", name="Game A", play_count=3)]


class FakeTrophyClientFactory:
    """Records every ``sub`` requested; raises ``RuntimeError`` for any ``sub`` not explicitly linked."""

    def __init__(self):
        self.linked: dict[str, FakeTrophyClient] = {}
        self.calls: list[str] = []

    async def __call__(self, sub):
        self.calls.append(sub)
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(f"No PSN link for user {sub!r}; cannot fetch trophies.")
        return client


def _build(trophy_client_factory=None, repository=None):
    settings = _make_settings()
    repository = repository if repository is not None else FakeRepository()
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(sub=SUB, email=EMAIL))
    app = create_app(
        settings,
        repository=repository,
        token_validator=validator,
        trophy_client_factory=trophy_client_factory or FakeTrophyClientFactory(),
    )
    return TestClient(app), app.state.trophy_client_factory


def _build_linked(trophy_client_factory=None):
    """Build an app whose caller has a PSN link with ``harvest_trophies`` enabled."""
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB, harvest_trophies=True)
    return _build(trophy_client_factory, repository=repository)


def test_trophy_summary_no_link_is_404():
    client, _ = _build()
    response = client.get("/trophies/summary", headers=_bearer("valid-token"))
    assert response.status_code == 404


def test_trophy_summary_happy_path():
    factory = FakeTrophyClientFactory()
    factory.linked[SUB] = FakeTrophyClient()
    client, _ = _build_linked(factory)

    response = client.get("/trophies/summary", headers=_bearer("valid-token"))

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "level": 42,
        "progress": 87,
        "tier": 3,
        "earned": {"bronze": 0, "silver": 0, "gold": 2, "platinum": 1},
        "account_id": "123",
    }
    assert factory.calls == [SUB]


def test_trophy_summary_psn_auth_error_is_401():
    factory = FakeTrophyClientFactory()
    factory.linked[SUB] = FakeTrophyClient(raise_auth_error=True)
    client, _ = _build_linked(factory)

    response = client.get("/trophies/summary", headers=_bearer("valid-token"))
    assert response.status_code == 401


def test_trophy_summary_harvest_trophies_disabled_is_403():
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB, harvest_trophies=False)
    client, _ = _build(repository=repository)

    response = client.get("/trophies/summary", headers=_bearer("valid-token"))
    assert response.status_code == 403


def test_trophy_titles_happy_path():
    factory = FakeTrophyClientFactory()
    factory.linked[SUB] = FakeTrophyClient()
    client, _ = _build_linked(factory)

    response = client.get("/trophies/titles?limit=25", headers=_bearer("valid-token"))

    assert response.status_code == 200
    titles = response.json()["titles"]
    assert len(titles) == 1
    assert titles[0]["np_communication_id"] == "NPWR1"
    assert titles[0]["platforms"] == ["PS5"]


def test_trophy_titles_harvest_trophies_disabled_is_403():
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB, harvest_trophies=False)
    client, _ = _build(repository=repository)

    response = client.get("/trophies/titles", headers=_bearer("valid-token"))
    assert response.status_code == 403


def test_title_trophies_requires_platform_query_param():
    factory = FakeTrophyClientFactory()
    factory.linked[SUB] = FakeTrophyClient()
    client, _ = _build_linked(factory)

    response = client.get("/trophies/titles/NPWR1", headers=_bearer("valid-token"))
    assert response.status_code == 422


def test_title_trophies_happy_path():
    factory = FakeTrophyClientFactory()
    fake_client = FakeTrophyClient()
    factory.linked[SUB] = fake_client
    client, _ = _build_linked(factory)

    response = client.get(
        "/trophies/titles/NPWR1", params={"platform": "PS5", "group": "default"}, headers=_bearer("valid-token")
    )

    assert response.status_code == 200
    trophies = response.json()["trophies"]
    assert trophies == [
        {
            "trophy_id": 1,
            "name": "First Blood",
            "detail": "Do the thing",
            "type": "bronze",
            "hidden": False,
            "icon_url": None,
            "earned": True,
            "earned_date": None,
            "progress_rate": None,
            "rarity": 42.5,
        }
    ]
    assert fake_client.title_trophies_calls == [("NPWR1", "PS5", "default")]


def test_title_trophies_harvest_trophies_disabled_is_403():
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB, harvest_trophies=False)
    client, _ = _build(repository=repository)

    response = client.get("/trophies/titles/NPWR1", params={"platform": "PS5"}, headers=_bearer("valid-token"))
    assert response.status_code == 403


def test_trophy_groups_happy_path():
    factory = FakeTrophyClientFactory()
    fake_client = FakeTrophyClient()
    factory.linked[SUB] = fake_client
    client, _ = _build_linked(factory)

    response = client.get("/trophies/titles/NPWR1/groups", params={"platform": "PS5"}, headers=_bearer("valid-token"))

    assert response.status_code == 200
    body = response.json()
    assert body["title_name"] == "Game A"
    assert body["groups"] == []
    assert fake_client.trophy_groups_calls == [("NPWR1", "PS5")]


def test_trophy_groups_no_link_is_404():
    client, _ = _build()
    response = client.get("/trophies/titles/NPWR1/groups", params={"platform": "PS5"}, headers=_bearer("valid-token"))
    assert response.status_code == 404


def test_trophy_groups_harvest_trophies_disabled_is_403():
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB, harvest_trophies=False)
    client, _ = _build(repository=repository)

    response = client.get("/trophies/titles/NPWR1/groups", params={"platform": "PS5"}, headers=_bearer("valid-token"))
    assert response.status_code == 403
