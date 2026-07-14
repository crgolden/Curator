"""Tests for Settings resolution: env vars / .env / missing-key errors.

Curator is a pure JWT Bearer resource server -- there is no OIDC client registration, so ``Settings`` only
resolves ``oidc_authority`` (for JWKS/issuer validation), ``token_key``, and ``database_url``.
"""

from __future__ import annotations

import pytest

from curator.persistence.config import ConfigError
from curator.settings import Settings


def _set_all_required(monkeypatch, **overrides):
    values = {
        "OIDC_AUTHORITY": "https://identity.example.test",
        "CURATOR_TOKEN_KEY": "token-key",
        "CURATOR_DATABASE_URL": "postgresql://curator",
    }
    values.update(overrides)
    for key, value in values.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.delenv("DATABASE_URL", raising=False)


def test_from_config_resolves_all_fields(monkeypatch, tmp_path):
    _set_all_required(monkeypatch)
    settings = Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert settings.oidc_authority == "https://identity.example.test"
    assert settings.token_key == "token-key"
    assert settings.database_url == "postgresql://curator"


def test_from_config_enrichment_and_jobs_settings_default_to_none(monkeypatch, tmp_path):
    _set_all_required(monkeypatch)
    for key in ("RawgApiKey", "OpenCriticRapidApiKey", "ServiceBusConnectionString"):
        monkeypatch.delenv(key, raising=False)

    settings = Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert settings.rawg_api_key is None
    assert settings.opencritic_rapidapi_key is None
    assert settings.service_bus_connection_string is None


def test_from_config_resolves_enrichment_and_jobs_settings_when_set(monkeypatch, tmp_path):
    _set_all_required(monkeypatch)
    monkeypatch.setenv("RawgApiKey", "rawg-key")
    monkeypatch.setenv("OpenCriticRapidApiKey", "oc-key")
    monkeypatch.setenv("ServiceBusConnectionString", "Endpoint=sb://example/;SharedAccessKey=x")

    settings = Settings.from_config(dotenv_path=tmp_path / "absent.env")

    assert settings.rawg_api_key == "rawg-key"
    assert settings.opencritic_rapidapi_key == "oc-key"
    assert settings.service_bus_connection_string == "Endpoint=sb://example/;SharedAccessKey=x"


@pytest.mark.parametrize("missing_key", ["OIDC_AUTHORITY", "CURATOR_TOKEN_KEY"])
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
