"""Tests for Settings resolution: env vars / .env / missing-key errors.

Curator is a pure JWT Bearer resource server -- there is no OIDC client registration, so ``Settings`` only
resolves ``oidc_authority`` (for JWKS/issuer validation), ``token_key``, and ``database_url``.
"""

from __future__ import annotations

import random

import pytest

from curator.persistence.config import ConfigError
from curator.persistence.connection import CURATOR_DATABASE_URL_ENV, DATABASE_URL_ENV
from curator.persistence.crypto import TOKEN_KEY_ENV
from curator.settings import (
    ALLOY_ENDPOINT,
    APP_SERVICE_SITE_NAME,
    ELASTICSEARCH_NODE,
    ELASTICSEARCH_PASSWORD,
    ELASTICSEARCH_USERNAME,
    OIDC_AUTHORITY,
    REDIS_HOST,
    REDIS_PASSWORD,
    REDIS_PORT,
    REDIS_SSL,
    SERVICE_BUS_CONNECTION_STRING,
    SERVICE_BUS_NAMESPACE,
    Settings,
)
from test_values import new_identity_sub

_TELEMETRY_KEYS = (ALLOY_ENDPOINT, ELASTICSEARCH_NODE, ELASTICSEARCH_USERNAME, ELASTICSEARCH_PASSWORD)
_TELEMETRY_URL_KEYS = (ALLOY_ENDPOINT, ELASTICSEARCH_NODE)

_AUTHORITY = f"https://{new_identity_sub()}.example.test"
_TOKEN_KEY = new_identity_sub()
_DATABASE_URL = f"postgresql://{new_identity_sub()}"


def _new_url() -> str:
    return f"https://{new_identity_sub()}.example.test:{random.randint(1024, 65535)}"


def _telemetry_values() -> dict[str, str | None]:
    return {
        ALLOY_ENDPOINT: _new_url(),
        ELASTICSEARCH_NODE: _new_url(),
        ELASTICSEARCH_USERNAME: new_identity_sub(),
        ELASTICSEARCH_PASSWORD: new_identity_sub(),
    }


def _set_all_required(monkeypatch, **overrides):
    values = {
        OIDC_AUTHORITY: _AUTHORITY,
        TOKEN_KEY_ENV: _TOKEN_KEY,
        CURATOR_DATABASE_URL_ENV: _DATABASE_URL,
        REDIS_HOST: "redis.example.test",
        REDIS_PORT: "6379",
        REDIS_SSL: "false",
        SERVICE_BUS_NAMESPACE: "bus.example.test",
        SERVICE_BUS_CONNECTION_STRING: None,
        REDIS_PASSWORD: None,
        APP_SERVICE_SITE_NAME: None,
        ALLOY_ENDPOINT: None,
        ELASTICSEARCH_NODE: None,
        ELASTICSEARCH_USERNAME: None,
        ELASTICSEARCH_PASSWORD: None,
    }
    values.update(overrides)
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)


def test_from_config_resolves_all_fields(monkeypatch, tmp_path):
    _set_all_required(monkeypatch)
    settings = Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert settings.oidc_authority == _AUTHORITY
    assert settings.token_key == _TOKEN_KEY
    assert settings.database_url == _DATABASE_URL


@pytest.mark.parametrize("missing_key", [REDIS_HOST, REDIS_PORT, REDIS_SSL])
def test_from_config_refuses_to_start_without_a_redis_setting(monkeypatch, tmp_path, missing_key):
    _set_all_required(monkeypatch, **{missing_key: None})

    with pytest.raises(ConfigError) as raised:
        Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert raised.value.setting == missing_key


@pytest.mark.parametrize("invalid_key", [REDIS_PORT, REDIS_SSL])
def test_from_config_refuses_a_redis_setting_it_cannot_parse(monkeypatch, tmp_path, invalid_key):
    _set_all_required(monkeypatch, **{invalid_key: new_identity_sub()})

    with pytest.raises(ConfigError) as raised:
        Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert raised.value.setting == invalid_key


def test_from_config_refuses_to_start_without_any_service_bus(monkeypatch, tmp_path):
    _set_all_required(monkeypatch, **{SERVICE_BUS_NAMESPACE: None, SERVICE_BUS_CONNECTION_STRING: None})

    with pytest.raises(ConfigError) as raised:
        Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert raised.value.setting == SERVICE_BUS_NAMESPACE


def test_from_config_accepts_a_service_bus_connection_string_without_a_namespace(monkeypatch, tmp_path):
    connection_string = new_identity_sub()
    _set_all_required(monkeypatch, **{SERVICE_BUS_NAMESPACE: None, SERVICE_BUS_CONNECTION_STRING: connection_string})

    settings = Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert settings.service_bus_connection_string == connection_string


def test_from_config_resolves_redis_settings_when_set(monkeypatch, tmp_path):
    host, password = new_identity_sub(), new_identity_sub()
    port = random.randint(1024, 65535)
    _set_all_required(
        monkeypatch, **{REDIS_HOST: host, REDIS_PORT: str(port), REDIS_PASSWORD: password, REDIS_SSL: "TRUE"}
    )

    settings = Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert settings.redis_host == host
    assert settings.redis_port == port
    assert settings.redis_password == password
    assert settings.redis_ssl is True


def test_from_config_leaves_telemetry_off_when_nothing_configures_it_off_app_service(monkeypatch, tmp_path):
    _set_all_required(monkeypatch)

    settings = Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert settings.alloy_endpoint is None
    assert settings.elasticsearch_node is None


def test_from_config_resolves_every_telemetry_setting_on_app_service(monkeypatch, tmp_path):
    telemetry = _telemetry_values()
    _set_all_required(monkeypatch, **{APP_SERVICE_SITE_NAME: new_identity_sub(), **telemetry})

    settings = Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert settings.alloy_endpoint == telemetry[ALLOY_ENDPOINT]
    assert settings.elasticsearch_node == telemetry[ELASTICSEARCH_NODE]
    assert settings.elasticsearch_username == telemetry[ELASTICSEARCH_USERNAME]
    assert settings.elasticsearch_password == telemetry[ELASTICSEARCH_PASSWORD]


@pytest.mark.parametrize("missing_key", _TELEMETRY_KEYS)
def test_from_config_refuses_to_start_on_app_service_without_a_telemetry_setting(monkeypatch, tmp_path, missing_key):
    telemetry = _telemetry_values()
    telemetry[missing_key] = None
    _set_all_required(monkeypatch, **{APP_SERVICE_SITE_NAME: new_identity_sub(), **telemetry})

    with pytest.raises(ConfigError) as raised:
        Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert raised.value.setting == missing_key


@pytest.mark.parametrize("credential_key", [ELASTICSEARCH_USERNAME, ELASTICSEARCH_PASSWORD])
def test_from_config_refuses_an_elasticsearch_node_without_its_credentials(monkeypatch, tmp_path, credential_key):
    telemetry = _telemetry_values()
    telemetry[credential_key] = None
    _set_all_required(monkeypatch, **telemetry)

    with pytest.raises(ConfigError) as raised:
        Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert raised.value.setting == credential_key


@pytest.mark.parametrize("url_key", _TELEMETRY_URL_KEYS)
def test_from_config_refuses_a_telemetry_endpoint_that_is_not_an_absolute_url(monkeypatch, tmp_path, url_key):
    telemetry = _telemetry_values()
    telemetry[url_key] = new_identity_sub()
    _set_all_required(monkeypatch, **telemetry)

    with pytest.raises(ConfigError) as raised:
        Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert raised.value.setting == url_key


@pytest.mark.parametrize("missing_key", [OIDC_AUTHORITY, TOKEN_KEY_ENV])
def test_from_config_raises_config_error_when_required_key_missing(monkeypatch, tmp_path, missing_key):
    _set_all_required(monkeypatch, **{missing_key: None})
    with pytest.raises(ConfigError):
        Settings.from_config(dotenv_path=tmp_path / "absent.env")


def test_from_config_raises_config_error_when_database_url_missing(monkeypatch, tmp_path):
    _set_all_required(monkeypatch, CURATOR_DATABASE_URL=None)
    with pytest.raises(ConfigError):
        Settings.from_config(dotenv_path=tmp_path / "absent.env")


def test_settings_is_frozen(monkeypatch, tmp_path):
    _set_all_required(monkeypatch)
    settings = Settings.from_config(dotenv_path=tmp_path / "absent.env")
    with pytest.raises(AttributeError):
        settings.token_key = "changed"  # type: ignore[misc] # intentional: proves the frozen dataclass raises at runtime
