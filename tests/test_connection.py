"""Tests for database URL resolution."""

from __future__ import annotations

import pytest

from curator.persistence.config import ConfigError
from curator.persistence.connection import resolve_database_url


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
