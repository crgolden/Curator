"""Tests for GET/PUT /games/{game_id}/measured-sizes[/{platform}], using create_app() with a fake
CollectionsRepository.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from curator import measured_sizes_routes
from curator.app import create_app
from curator.collections.repository import MeasuredSize
from curator.measured_sizes_routes import MeasuredSizeResponse, SetMeasuredSizeRequest
from curator.persistence.crypto import TokenCrypto
from curator.psn.title_platform import PS1, PS2, PS3, PS4, PS5, PSP, PSVITA, platform_vocabulary_message
from test_routes import FakeAgentFactory, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _path
from test_values import lowercase_token, new_game_id, new_identity_sub, new_opaque_token, new_size_gb, new_utc_instant

SUB_A = new_identity_sub()
SUB_B = new_identity_sub()
TOKEN_A = new_opaque_token()
TOKEN_B = new_opaque_token()

_FIXED_NOW = new_utc_instant()

_SIZES = TypeAdapter(list[MeasuredSizeResponse])


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


def _list_path(client, game_id: str) -> str:
    return _path(client, measured_sizes_routes.list_measured_sizes, game_id=game_id)


def _set_path(client, game_id: str, platform: str) -> str:
    return _path(client, measured_sizes_routes.set_measured_size, game_id=game_id, platform=platform)


def _size_body(size_gb: float) -> dict[str, object]:
    return SetMeasuredSizeRequest(size_gb=size_gb).model_dump()


def _response(measured_size: MeasuredSize) -> MeasuredSizeResponse:
    return MeasuredSizeResponse(
        game_id=measured_size.game_id,
        platform=measured_size.platform,
        size_gb=measured_size.size_gb,
        recorded_by=measured_size.recorded_by,
        recorded_at=measured_size.recorded_at.isoformat(),
    )


def test_requires_bearer_token():
    client, _validator = _build()

    response = client.get(_list_path(client, new_game_id()))

    assert response.status_code == 401


def test_lists_no_measured_sizes_for_a_game_nobody_has_measured():
    client, validator = _build()
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.get(_list_path(client, new_game_id()), headers=_bearer(TOKEN_A))

    assert response.status_code == 200
    assert _SIZES.validate_python(response.json()) == []


def test_sets_a_measured_size_recorded_by_the_caller():
    game_id = new_game_id()
    size_gb = new_size_gb()
    repo = FakeCollectionsRepository()
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.put(_set_path(client, game_id, PS5), json=_size_body(size_gb), headers=_bearer(TOKEN_A))

    assert response.status_code == 200
    assert MeasuredSizeResponse.model_validate(response.json()) == MeasuredSizeResponse(
        game_id=game_id,
        platform=PS5,
        size_gb=size_gb,
        recorded_by=SUB_A,
        recorded_at=_FIXED_NOW.isoformat(),
    )
    assert repo.upsert_calls == [(game_id, PS5, size_gb, SUB_A)]


def test_any_authenticated_user_may_contribute_a_measured_size_not_only_the_first_contributor():
    game_id = new_game_id()
    contributed_size_gb = new_size_gb()
    repo = FakeCollectionsRepository(
        sizes=[
            MeasuredSize(
                game_id=game_id, platform=PS5, size_gb=new_size_gb(), recorded_by=SUB_A, recorded_at=_FIXED_NOW
            )
        ]
    )
    client, validator = _build(repo)
    validator.register(TOKEN_B, _claims(sub=SUB_B))

    response = client.put(
        _set_path(client, game_id, PS5), json=_size_body(contributed_size_gb), headers=_bearer(TOKEN_B)
    )

    assert response.status_code == 200
    body = MeasuredSizeResponse.model_validate(response.json())
    assert body.recorded_by == SUB_B
    assert body.size_gb == contributed_size_gb


def test_rejects_a_platform_outside_the_platforms_table():
    client, validator = _build()
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.put(
        _set_path(client, new_game_id(), lowercase_token()), json=_size_body(new_size_gb()), headers=_bearer(TOKEN_A)
    )

    assert response.status_code == 400
    assert response.json()["detail"] == platform_vocabulary_message()


@pytest.mark.parametrize("platform", [PS3, PSVITA, PSP, PS2, PS1])
def test_accepts_a_legacy_platform_the_schema_already_allows(platform):
    """game_measured_sizes.platform is a foreign key to platforms, which carries seven rows. Narrowing
    the route to the PS5/PS4 pair rejected five platforms the database was happy to store."""
    client, validator = _build()
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.put(
        _set_path(client, new_game_id(), platform), json=_size_body(new_size_gb()), headers=_bearer(TOKEN_A)
    )

    assert response.status_code == 200
    assert MeasuredSizeResponse.model_validate(response.json()).platform == platform


def test_lists_measured_sizes_for_both_platforms():
    game_id = new_game_id()
    ps4_size = MeasuredSize(
        game_id=game_id, platform=PS4, size_gb=new_size_gb(), recorded_by=SUB_A, recorded_at=_FIXED_NOW
    )
    ps5_size = MeasuredSize(
        game_id=game_id, platform=PS5, size_gb=new_size_gb(), recorded_by=SUB_A, recorded_at=_FIXED_NOW
    )
    repo = FakeCollectionsRepository(sizes=[ps4_size, ps5_size])
    client, validator = _build(repo)
    validator.register(TOKEN_A, _claims(sub=SUB_A))

    response = client.get(_list_path(client, game_id), headers=_bearer(TOKEN_A))

    assert response.status_code == 200
    assert _SIZES.validate_python(response.json()) == [_response(ps4_size), _response(ps5_size)]
