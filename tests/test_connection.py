"""Tests for database URL resolution and the CA bundle a verifying URL is given."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import certifi
import pytest
from psycopg.conninfo import conninfo_to_dict

from curator.persistence.config import ConfigError
from curator.persistence.connection import (
    CERTIFICATE_VERIFYING_SSL_MODES,
    CURATOR_DATABASE_URL_ENV,
    DATABASE_URL_ENV,
    SSLMODE_PARAM,
    SSLROOTCERT_PARAM,
    VERIFY_FULL_SSL_MODE,
    resolve_database_url,
    with_root_certificate,
)


def _new_identifier() -> str:
    return f"id{uuid4().hex}"


def _new_url(query: str = "") -> str:
    return (
        f"postgresql://{_new_identifier()}:{_new_identifier()}@{_new_identifier()}.test:5432/{_new_identifier()}{query}"
    )


def _clear_database_env(monkeypatch) -> None:
    monkeypatch.delenv(CURATOR_DATABASE_URL_ENV, raising=False)
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)


def test_resolve_database_url_prefers_explicit(monkeypatch):
    explicit = _new_url()
    monkeypatch.setenv(CURATOR_DATABASE_URL_ENV, _new_url())
    assert resolve_database_url(explicit) == explicit


def test_resolve_database_url_reads_curator_env_var(monkeypatch, tmp_path):
    _clear_database_env(monkeypatch)
    configured = _new_url()
    monkeypatch.setenv(CURATOR_DATABASE_URL_ENV, configured)
    assert resolve_database_url(dotenv_path=tmp_path / "absent.env") == configured


def test_resolve_database_url_falls_back_to_generic_env_var(monkeypatch, tmp_path):
    _clear_database_env(monkeypatch)
    configured = _new_url()
    monkeypatch.setenv(DATABASE_URL_ENV, configured)
    assert resolve_database_url(dotenv_path=tmp_path / "absent.env") == configured


def test_resolve_database_url_reads_dotenv(monkeypatch, tmp_path):
    _clear_database_env(monkeypatch)
    configured = _new_url()
    dotenv = tmp_path / ".env"
    dotenv.write_text(f'{CURATOR_DATABASE_URL_ENV}="{configured}"\n', encoding="utf-8")
    assert resolve_database_url(dotenv_path=dotenv) == configured


def test_resolve_database_url_missing_raises(monkeypatch, tmp_path):
    _clear_database_env(monkeypatch)
    with pytest.raises(ConfigError):
        resolve_database_url(dotenv_path=tmp_path / "absent.env")


@pytest.mark.parametrize("ssl_mode", sorted(CERTIFICATE_VERIFYING_SSL_MODES))
def test_a_verifying_ssl_mode_is_given_the_certifi_bundle(ssl_mode):
    parameters = conninfo_to_dict(with_root_certificate(_new_url(f"?{SSLMODE_PARAM}={ssl_mode}")))
    assert parameters[SSLMODE_PARAM] == ssl_mode
    assert parameters[SSLROOTCERT_PARAM] == certifi.where()


@pytest.mark.parametrize("ssl_mode", ["disable", "allow", "prefer", "require"])
def test_a_non_verifying_ssl_mode_is_left_exactly_as_configured(ssl_mode):
    url = _new_url(f"?{SSLMODE_PARAM}={ssl_mode}")
    assert with_root_certificate(url) == url


def test_a_url_naming_no_ssl_mode_is_left_exactly_as_configured():
    url = _new_url()
    assert with_root_certificate(url) == url


def test_a_url_naming_its_own_root_certificate_keeps_it():
    configured_bundle = f"/etc/ssl/{_new_identifier()}.crt"
    url = _new_url(f"?{SSLMODE_PARAM}={VERIFY_FULL_SSL_MODE}&{SSLROOTCERT_PARAM}={configured_bundle}")
    assert conninfo_to_dict(with_root_certificate(url))[SSLROOTCERT_PARAM] == configured_bundle


def test_adding_the_bundle_preserves_every_other_connection_parameter():
    user = _new_identifier()
    password = _new_identifier()
    host = f"{_new_identifier()}.test"
    dbname = _new_identifier()

    parameters = conninfo_to_dict(
        with_root_certificate(
            f"postgresql://{user}:{password}@{host}:5432/{dbname}?{SSLMODE_PARAM}={VERIFY_FULL_SSL_MODE}"
        )
    )

    assert parameters["user"] == user
    assert parameters["password"] == password
    assert parameters["host"] == host
    assert parameters["port"] == "5432"
    assert parameters["dbname"] == dbname


def test_resolve_database_url_gives_a_verifying_url_the_bundle(monkeypatch, tmp_path):
    monkeypatch.delenv(DATABASE_URL_ENV, raising=False)
    monkeypatch.setenv(CURATOR_DATABASE_URL_ENV, _new_url(f"?{SSLMODE_PARAM}={VERIFY_FULL_SSL_MODE}"))

    resolved = resolve_database_url(dotenv_path=tmp_path / "absent.env")

    assert conninfo_to_dict(resolved)[SSLROOTCERT_PARAM] == certifi.where()


def test_the_named_bundle_is_a_readable_file_holding_certificates():
    assert "BEGIN CERTIFICATE" in Path(certifi.where()).read_text(encoding="ascii")
