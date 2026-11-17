"""Tests for the generic arg -> env var -> .env resolution helper."""

from __future__ import annotations

from curator.persistence.config import _read_dotenv, resolve_setting
from test_values import lowercase_token, new_env_var_name

_LINE_END = "\n"
_ENCODING = "utf-8"


def test_read_dotenv_missing_file_returns_empty(tmp_path):
    assert _read_dotenv(tmp_path / lowercase_token()) == {}


def test_read_dotenv_skips_blanks_and_comments(tmp_path):
    first_key, first_value = new_env_var_name(), lowercase_token()
    second_key, second_value = new_env_var_name(), lowercase_token()
    dotenv = tmp_path / lowercase_token()
    dotenv.write_text(
        f"{_LINE_END}# {lowercase_token()}{_LINE_END}{first_key}={first_value}{_LINE_END}{_LINE_END}"
        f"# {lowercase_token()}{_LINE_END}{second_key}={second_value}{_LINE_END}",
        encoding=_ENCODING,
    )
    assert _read_dotenv(dotenv) == {first_key: first_value, second_key: second_value}


def test_read_dotenv_strips_optional_quotes(tmp_path):
    double_key, double_value = new_env_var_name(), lowercase_token()
    single_key, single_value = new_env_var_name(), lowercase_token()
    bare_key, bare_value = new_env_var_name(), lowercase_token()
    dotenv = tmp_path / lowercase_token()
    lines = [f'{double_key}="{double_value}"', f"{single_key}='{single_value}'", f"{bare_key}={bare_value}"]
    dotenv.write_text(f"{_LINE_END.join(lines)}{_LINE_END}", encoding=_ENCODING)
    assert _read_dotenv(dotenv) == {
        double_key: double_value,
        single_key: single_value,
        bare_key: bare_value,
    }


def test_read_dotenv_ignores_lines_without_equals(tmp_path):
    key, value = new_env_var_name(), lowercase_token()
    dotenv = tmp_path / lowercase_token()
    dotenv.write_text(f"{new_env_var_name()}{_LINE_END}{key}={value}{_LINE_END}", encoding=_ENCODING)
    assert _read_dotenv(dotenv) == {key: value}


def test_resolve_setting_prefers_explicit(monkeypatch):
    name = new_env_var_name()
    explicit = lowercase_token()
    monkeypatch.setenv(name, lowercase_token())
    assert resolve_setting(explicit, env_names=(name,)) == explicit


def test_resolve_setting_env_wins_over_dotenv(tmp_path, monkeypatch):
    name = new_env_var_name()
    from_env = lowercase_token()
    monkeypatch.setenv(name, from_env)
    dotenv = tmp_path / lowercase_token()
    dotenv.write_text(f"{name}={lowercase_token()}{_LINE_END}", encoding=_ENCODING)
    assert resolve_setting(None, env_names=(name,), dotenv_path=dotenv) == from_env


def test_resolve_setting_reads_dotenv_when_env_absent(tmp_path, monkeypatch):
    name = new_env_var_name()
    from_dotenv = lowercase_token()
    monkeypatch.delenv(name, raising=False)
    dotenv = tmp_path / lowercase_token()
    dotenv.write_text(f"{name}={from_dotenv}{_LINE_END}", encoding=_ENCODING)
    assert resolve_setting(None, env_names=(name,), dotenv_path=dotenv) == from_dotenv


def test_resolve_setting_tries_multiple_env_names_in_order(monkeypatch):
    first_name = new_env_var_name()
    second_name = new_env_var_name()
    second_value = lowercase_token()
    monkeypatch.delenv(first_name, raising=False)
    monkeypatch.setenv(second_name, second_value)
    assert resolve_setting(None, env_names=(first_name, second_name)) == second_value


def test_resolve_setting_returns_none_when_nothing_found(tmp_path, monkeypatch):
    name = new_env_var_name()
    monkeypatch.delenv(name, raising=False)
    assert resolve_setting(None, env_names=(name,), dotenv_path=tmp_path / lowercase_token()) is None
