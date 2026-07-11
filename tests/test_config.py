"""Tests for the generic arg -> env var -> .env resolution helper."""

from __future__ import annotations

from curator.persistence.config import _read_dotenv, resolve_setting


def test_read_dotenv_missing_file_returns_empty(tmp_path):
    assert _read_dotenv(tmp_path / "absent.env") == {}


def test_read_dotenv_skips_blanks_and_comments(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "\n# a comment\nKEY_ONE=value1\n\n# another\nKEY_TWO=value2\n",
        encoding="utf-8",
    )
    assert _read_dotenv(dotenv) == {"KEY_ONE": "value1", "KEY_TWO": "value2"}


def test_read_dotenv_strips_optional_quotes(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text('DOUBLE="double-value"\nSINGLE=\'single-value\'\nBARE=bare-value\n', encoding="utf-8")
    assert _read_dotenv(dotenv) == {
        "DOUBLE": "double-value",
        "SINGLE": "single-value",
        "BARE": "bare-value",
    }


def test_read_dotenv_ignores_lines_without_equals(tmp_path):
    dotenv = tmp_path / ".env"
    dotenv.write_text("NOT_A_SETTING\nKEY=value\n", encoding="utf-8")
    assert _read_dotenv(dotenv) == {"KEY": "value"}


def test_resolve_setting_prefers_explicit(monkeypatch):
    monkeypatch.setenv("SOME_VAR", "from-env")
    assert resolve_setting("explicit-value", env_names=("SOME_VAR",)) == "explicit-value"


def test_resolve_setting_env_wins_over_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_VAR", "from-env")
    dotenv = tmp_path / ".env"
    dotenv.write_text("SOME_VAR=from-dotenv\n", encoding="utf-8")
    assert resolve_setting(None, env_names=("SOME_VAR",), dotenv_path=dotenv) == "from-env"


def test_resolve_setting_reads_dotenv_when_env_absent(tmp_path, monkeypatch):
    monkeypatch.delenv("SOME_VAR", raising=False)
    dotenv = tmp_path / ".env"
    dotenv.write_text("SOME_VAR=from-dotenv\n", encoding="utf-8")
    assert resolve_setting(None, env_names=("SOME_VAR",), dotenv_path=dotenv) == "from-dotenv"


def test_resolve_setting_tries_multiple_env_names_in_order(monkeypatch):
    monkeypatch.delenv("FIRST_VAR", raising=False)
    monkeypatch.setenv("SECOND_VAR", "second-value")
    assert resolve_setting(None, env_names=("FIRST_VAR", "SECOND_VAR")) == "second-value"


def test_resolve_setting_returns_none_when_nothing_found(tmp_path, monkeypatch):
    monkeypatch.delenv("SOME_VAR", raising=False)
    assert resolve_setting(None, env_names=("SOME_VAR",), dotenv_path=tmp_path / "absent.env") is None
