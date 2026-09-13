"""Tests for database URL resolution and the CA bundle a verifying URL is given."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import certifi
import pytest
from psycopg.conninfo import conninfo_to_dict

from curator.persistence.config import ConfigError
from curator.persistence.connection import resolve_database_url, with_root_certificate


def _new_identifier() -> str:
    return f"id{uuid4().hex}"


def _new_url(query: str = "") -> str:
    return (
        f"postgresql://{_new_identifier()}:{_new_identifier()}@{_new_identifier()}.test:5432/{_new_identifier()}{query}"
    )


def test_resolve_database_url_prefers_explicit(monkeypatch):
    monkeypatch.setenv("CURATOR_DATABASE_URL", "postgresql://from-env")
    assert resolve_database_url("postgresql://explicit") == "postgresql://explicit"


def test_resolve_database_url_reads_curator_env_var(monkeypatch, tmp_path):
    monkeypatch.delenv("CURATOR_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("CURATOR_DATABASE_URL", "postgresql://curator-env")
    assert resolve_database_url(dotenv_path=tmp_path / "absent.env") == "postgresql://curator-env"


def test_resolve_database_url_falls_back_to_generic_env_var(monkeypatch, tmp_path):
    monkeypatch.delenv("CURATOR_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://generic-env")
    assert resolve_database_url(dotenv_path=tmp_path / "absent.env") == "postgresql://generic-env"


def test_resolve_database_url_reads_dotenv(monkeypatch, tmp_path):
    monkeypatch.delenv("CURATOR_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text('CURATOR_DATABASE_URL="postgresql://dotenv-value"\n', encoding="utf-8")
    assert resolve_database_url(dotenv_path=dotenv) == "postgresql://dotenv-value"


def test_resolve_database_url_missing_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("CURATOR_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ConfigError):
        resolve_database_url(dotenv_path=tmp_path / "absent.env")


@pytest.mark.parametrize("ssl_mode", ["verify-ca", "verify-full"])
def test_a_verifying_ssl_mode_is_given_the_certifi_bundle(ssl_mode):
    parameters = conninfo_to_dict(with_root_certificate(_new_url(f"?sslmode={ssl_mode}")))
    assert parameters["sslmode"] == ssl_mode
    assert parameters["sslrootcert"] == certifi.where()


@pytest.mark.parametrize("ssl_mode", ["disable", "allow", "prefer", "require"])
def test_a_non_verifying_ssl_mode_is_left_exactly_as_configured(ssl_mode):
    url = _new_url(f"?sslmode={ssl_mode}")
    assert with_root_certificate(url) == url


def test_a_url_naming_no_ssl_mode_is_left_exactly_as_configured():
    url = _new_url()
    assert with_root_certificate(url) == url


def test_a_url_naming_its_own_root_certificate_keeps_it():
    configured_bundle = f"/etc/ssl/{_new_identifier()}.crt"
    url = _new_url(f"?sslmode=verify-full&sslrootcert={configured_bundle}")
    assert conninfo_to_dict(with_root_certificate(url))["sslrootcert"] == configured_bundle


def test_adding_the_bundle_preserves_every_other_connection_parameter():
    user = _new_identifier()
    password = _new_identifier()
    host = f"{_new_identifier()}.test"
    dbname = _new_identifier()

    parameters = conninfo_to_dict(
        with_root_certificate(f"postgresql://{user}:{password}@{host}:5432/{dbname}?sslmode=verify-full")
    )

    assert parameters["user"] == user
    assert parameters["password"] == password
    assert parameters["host"] == host
    assert parameters["port"] == "5432"
    assert parameters["dbname"] == dbname


def test_resolve_database_url_gives_a_verifying_url_the_bundle(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("CURATOR_DATABASE_URL", _new_url("?sslmode=verify-full"))

    resolved = resolve_database_url(dotenv_path=tmp_path / "absent.env")

    assert conninfo_to_dict(resolved)["sslrootcert"] == certifi.where()


def test_the_named_bundle_is_a_readable_file_holding_certificates():
    assert "BEGIN CERTIFICATE" in Path(certifi.where()).read_text(encoding="ascii")
