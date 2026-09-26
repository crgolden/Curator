"""Tests for GET/PUT /me/psn-preferences -- create_app wired with FakeRepository (the same DI-seam style
as test_trophy_routes.py). Unlike the other new PSN-data routes, these two aren't gated by
require_preference -- only unlinked (404) and linked (200) cases apply.
"""

from __future__ import annotations

import random

from fastapi.testclient import TestClient

from curator import preferences_routes
from curator.app import create_app
from curator.deps import PREFERENCE_NOT_LINKED_DETAIL
from curator.persistence.crypto import TokenCrypto
from curator.preferences_routes import PsnPreferences
from test_routes import (
    FakeLibraryRepository,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _path,
    _seed_link,
)
from test_values import new_email_address, new_flag, new_identity_sub, new_opaque_token


class _Caller:
    def __init__(self) -> None:
        self.sub = new_identity_sub()
        self.token = new_opaque_token()


def _build(caller: _Caller, repository=None):
    settings = _make_settings()
    repository = repository if repository is not None else FakeRepository()
    validator = FakeTokenValidator()
    validator.register(caller.token, _claims(sub=caller.sub, email=new_email_address()))
    app = create_app(settings, repository=repository, token_validator=validator)
    library_repository = FakeLibraryRepository()
    app.state.library_repository = library_repository
    return TestClient(app), repository, library_repository


def _build_linked(caller: _Caller, **harvest_flags):
    repository = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    _seed_link(repository, crypto, caller.sub, **harvest_flags)
    return _build(caller, repository)


def _preferences_path(client: TestClient) -> str:
    return _path(client, preferences_routes.get_psn_preferences)


def _harvest_flags_with_trophies(harvest_trophies: bool) -> PsnPreferences:
    return PsnPreferences(
        harvest_trophies=harvest_trophies,
        harvest_identity=new_flag(),
        harvest_presence=new_flag(),
        harvest_devices=new_flag(),
    )


def _generated_harvest_flags() -> PsnPreferences:
    return _harvest_flags_with_trophies(new_flag())


def _body_missing_one_required_flag() -> dict[str, object]:
    body = _generated_harvest_flags().model_dump()
    required = [name for name, field in PsnPreferences.model_fields.items() if field.is_required()]
    body.pop(random.choice(required))
    return body


def test_get_psn_preferences_no_link_is_404():
    caller = _Caller()
    client, *_ = _build(caller)

    response = client.get(_preferences_path(client), headers=_bearer(caller.token))

    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL


def test_get_psn_preferences_happy_path():
    caller = _Caller()
    stored = _generated_harvest_flags()
    client, *_ = _build_linked(
        caller,
        harvest_trophies=stored.harvest_trophies,
        harvest_identity=stored.harvest_identity,
        harvest_presence=stored.harvest_presence,
        harvest_devices=stored.harvest_devices,
    )

    response = client.get(_preferences_path(client), headers=_bearer(caller.token))

    assert response.status_code == 200
    assert PsnPreferences.model_validate(response.json()) == stored


def test_get_psn_preferences_defaults_all_false():
    caller = _Caller()
    client, *_ = _build_linked(caller)

    response = client.get(_preferences_path(client), headers=_bearer(caller.token))

    assert response.status_code == 200
    assert PsnPreferences.model_validate(response.json()) == PsnPreferences(
        harvest_trophies=False,
        harvest_identity=False,
        harvest_presence=False,
        harvest_devices=False,
        allow_friend_writes=False,
        allow_chat_writes=False,
    )


def test_put_psn_preferences_no_link_is_404():
    caller = _Caller()
    client, *_ = _build(caller)

    response = client.put(
        _path(client, preferences_routes.set_psn_preferences),
        json=_generated_harvest_flags().model_dump(),
        headers=_bearer(caller.token),
    )

    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL


def test_put_psn_preferences_happy_path():
    caller = _Caller()
    client, repository, _ = _build_linked(caller)
    requested = _generated_harvest_flags()

    response = client.put(
        _path(client, preferences_routes.set_psn_preferences),
        json=requested.model_dump(),
        headers=_bearer(caller.token),
    )

    assert response.status_code == 200
    assert PsnPreferences.model_validate(response.json()) == requested
    assert repository.set_psn_preferences_calls == [
        (
            caller.sub,
            requested.harvest_trophies,
            requested.harvest_identity,
            requested.harvest_presence,
            requested.harvest_devices,
        )
    ]
    assert repository.links[caller.sub].harvest_trophies is requested.harvest_trophies
    assert repository.links[caller.sub].harvest_presence is requested.harvest_presence


def test_put_psn_preferences_requires_all_four_fields():
    caller = _Caller()
    client, *_ = _build_linked(caller)

    response = client.put(
        _path(client, preferences_routes.set_psn_preferences),
        json=_body_missing_one_required_flag(),
        headers=_bearer(caller.token),
    )

    assert response.status_code == 422


def test_put_psn_preferences_turning_trophies_off_clears_stored_progress():
    """Opting out has to erase what was already collected, not merely stop collecting.

    This is the promise /privacy makes in as many words ("turning trophy harvesting back off erases those
    numbers, it doesn't just stop refreshing them"), so it needs an assertion holding it in place -- the
    route can silently lose this branch and every other test here would still pass.
    """
    caller = _Caller()
    client, _, library_repository = _build_linked(caller, harvest_trophies=True)
    requested = _harvest_flags_with_trophies(False)

    response = client.put(
        _path(client, preferences_routes.set_psn_preferences),
        json=requested.model_dump(),
        headers=_bearer(caller.token),
    )

    assert response.status_code == 200
    assert library_repository.clear_trophy_progress_calls == [caller.sub]


def test_put_psn_preferences_leaving_trophies_on_does_not_clear_progress():
    """Only the on-to-off transition erases. Re-saving preferences with trophies still on must not wipe a
    user's progress as a side effect of toggling an unrelated flag.
    """
    caller = _Caller()
    client, _, library_repository = _build_linked(caller, harvest_trophies=True)
    requested = _harvest_flags_with_trophies(True)

    response = client.put(
        _path(client, preferences_routes.set_psn_preferences),
        json=requested.model_dump(),
        headers=_bearer(caller.token),
    )

    assert response.status_code == 200
    assert library_repository.clear_trophy_progress_calls == []
