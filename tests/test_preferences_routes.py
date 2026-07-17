"""Tests for GET/PUT /me/psn-preferences -- create_app wired with FakeRepository (the same DI-seam style
as test_trophy_routes.py). Unlike the other new PSN-data routes, these two aren't gated by
require_preference -- only unlinked (404) and linked (200) cases apply.
"""

from __future__ import annotations

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from test_routes import EMAIL, SUB, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings, _seed_link


def _build(repository=None):
    settings = _make_settings()
    repository = repository if repository is not None else FakeRepository()
    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(sub=SUB, email=EMAIL))
    app = create_app(settings, repository=repository, token_validator=validator)
    return TestClient(app), repository


def _build_linked(**harvest_flags):
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, SUB, **harvest_flags)
    return _build(repository)


def test_get_psn_preferences_no_link_is_404():
    client, _ = _build()
    response = client.get("/me/psn-preferences", headers=_bearer("valid-token"))
    assert response.status_code == 404


def test_get_psn_preferences_happy_path():
    client, _ = _build_linked(
        harvest_trophies=True, harvest_identity=False, harvest_presence=True, harvest_devices=False
    )
    response = client.get("/me/psn-preferences", headers=_bearer("valid-token"))

    assert response.status_code == 200
    assert response.json() == {
        "harvest_trophies": True,
        "harvest_identity": False,
        "harvest_presence": True,
        "harvest_devices": False,
    }


def test_get_psn_preferences_defaults_all_false():
    client, _ = _build_linked()
    response = client.get("/me/psn-preferences", headers=_bearer("valid-token"))

    assert response.status_code == 200
    assert response.json() == {
        "harvest_trophies": False,
        "harvest_identity": False,
        "harvest_presence": False,
        "harvest_devices": False,
    }


def test_put_psn_preferences_no_link_is_404():
    client, _ = _build()
    response = client.put(
        "/me/psn-preferences",
        json={
            "harvest_trophies": True,
            "harvest_identity": True,
            "harvest_presence": True,
            "harvest_devices": True,
        },
        headers=_bearer("valid-token"),
    )
    assert response.status_code == 404


def test_put_psn_preferences_happy_path():
    client, repository = _build_linked()
    body = {
        "harvest_trophies": True,
        "harvest_identity": False,
        "harvest_presence": True,
        "harvest_devices": False,
    }
    response = client.put("/me/psn-preferences", json=body, headers=_bearer("valid-token"))

    assert response.status_code == 200
    assert response.json() == body
    assert repository.set_psn_preferences_calls == [(SUB, True, False, True, False)]
    assert repository.links[SUB].harvest_trophies is True
    assert repository.links[SUB].harvest_presence is True


def test_put_psn_preferences_requires_all_four_fields():
    client, _ = _build_linked()
    response = client.put(
        "/me/psn-preferences",
        json={"harvest_trophies": True},
        headers=_bearer("valid-token"),
    )
    assert response.status_code == 422
