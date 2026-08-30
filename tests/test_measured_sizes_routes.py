"""Tests for GET/PUT /games/{game_id}/measured-sizes[/{platform}], using create_app() with a fake
CollectionsRepository.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.collections.repository import MeasuredSize
from curator.persistence.crypto import TokenCrypto
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings

_FIXED_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeCollectionsRepository:
    """Only the two methods this route family calls -- same one-purpose-per-test-file convention as
    ``test_storage_devices_routes.FakeCollectionsRepository``."""

    def __init__(self, sizes=None):
        self._sizes: dict[tuple[str, str], MeasuredSize] = {(s.game_id, s.platform): s for s in (sizes or [])}
        self.upsert_calls: list[tuple[str, str, float, str]] = []

    async def list_measured_sizes(self, game_id):
        return sorted((s for (gid, _platform), s in self._sizes.items() if gid == game_id), key=lambda s: s.platform)

    async def upsert_measured_size(self, game_id, platform, size_gb, recorded_by):
        self.upsert_calls.append((game_id, platform, size_gb, recorded_by))
        measured_size = MeasuredSize(
            game_id=game_id, platform=platform, size_gb=size_gb, recorded_by=recorded_by, recorded_at=_FIXED_NOW
        )
        self._sizes[(game_id, platform)] = measured_size
        return measured_size


def _build(collections_repository=None):
    repository = FakeRepository()
    token_crypto = TokenCrypto(TokenCrypto.generate_key())
    validator = FakeTokenValidator()
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=validator,
        collections_repository=collections_repository or FakeCollectionsRepository(),
    )
    return TestClient(app), validator


def test_requires_bearer_token():
    client, _validator = _build()

    response = client.get("/games/g1/measured-sizes")

    assert response.status_code == 401


def test_lists_no_measured_sizes_for_a_game_nobody_has_measured():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/games/g1/measured-sizes", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == []


def test_sets_a_measured_size_recorded_by_the_caller():
    repo = FakeCollectionsRepository()
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put("/games/g1/measured-sizes/PS5", json={"size_gb": 42.5}, headers=_bearer("token-a"))

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "game_id": "g1",
        "platform": "PS5",
        "size_gb": 42.5,
        "recorded_by": "sub-a",
        "recorded_at": _FIXED_NOW.isoformat(),
    }
    assert repo.upsert_calls == [("g1", "PS5", 42.5, "sub-a")]


def test_any_authenticated_user_may_contribute_a_measured_size_not_only_the_first_contributor():
    repo = FakeCollectionsRepository(
        sizes=[MeasuredSize(game_id="g1", platform="PS5", size_gb=40.0, recorded_by="sub-a", recorded_at=_FIXED_NOW)]
    )
    client, validator = _build(repo)
    validator.register("token-b", _claims(sub="sub-b"))

    response = client.put("/games/g1/measured-sizes/PS5", json={"size_gb": 45.0}, headers=_bearer("token-b"))

    assert response.status_code == 200
    assert response.json()["recorded_by"] == "sub-b"
    assert response.json()["size_gb"] == 45.0


def test_rejects_a_platform_outside_the_platforms_table():
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put("/games/g1/measured-sizes/Switch", json={"size_gb": 42.5}, headers=_bearer("token-a"))

    assert response.status_code == 400


@pytest.mark.parametrize("platform", ["PS3", "PSVITA", "PSP", "PS2", "PS1"])
def test_accepts_a_legacy_platform_the_schema_already_allows(platform):
    """game_measured_sizes.platform is a foreign key to platforms, which carries seven rows. Narrowing
    the route to the PS5/PS4 pair rejected five platforms the database was happy to store."""
    client, validator = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.put(f"/games/g1/measured-sizes/{platform}", json={"size_gb": 8.5}, headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json()["platform"] == platform


def test_lists_measured_sizes_for_both_platforms():
    repo = FakeCollectionsRepository(
        sizes=[
            MeasuredSize(game_id="g1", platform="PS4", size_gb=30.0, recorded_by="sub-a", recorded_at=_FIXED_NOW),
            MeasuredSize(game_id="g1", platform="PS5", size_gb=42.5, recorded_by="sub-a", recorded_at=_FIXED_NOW),
        ]
    )
    client, validator = _build(repo)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/games/g1/measured-sizes", headers=_bearer("token-a"))

    assert response.status_code == 200
    platforms = [row["platform"] for row in response.json()]
    assert platforms == ["PS4", "PS5"]
