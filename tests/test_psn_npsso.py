"""Tests for parse_npsso(), ported from psnpy's test_config.py -- only the parse_npsso subset. The
resolve_npsso()/npsso_env_names() env-var resolution chain is intentionally NOT ported: Curator always
receives the npsso per-request via POST /psn/link's body, so there is no "resolve from environment"
concept to preserve (see the migration plan's psnpy capability audit)."""

from __future__ import annotations

import json

import pytest

from curator.psn.npsso import NpssoError, parse_npsso
from test_values import lowercase_token, new_blank, new_npsso


def test_parse_npsso_raw_token_passthrough():
    token = new_npsso()
    assert parse_npsso(token) == token


def test_parse_npsso_unwraps_json_blob():
    token = new_npsso()
    assert parse_npsso(json.dumps({"npsso": token})) == token


def test_parse_npsso_malformed_json_raises():
    malformed = f"{{{new_npsso()}"
    with pytest.raises(NpssoError):
        parse_npsso(malformed)


def test_parse_npsso_missing_key_raises():
    without_npsso_key = json.dumps({lowercase_token(): lowercase_token()})
    with pytest.raises(NpssoError):
        parse_npsso(without_npsso_key)


def test_parse_npsso_strips_whitespace():
    token = new_npsso()
    assert parse_npsso(f"{new_blank()}{token}{new_blank()}") == token


def test_parse_npsso_empty_npsso_value_raises():
    with pytest.raises(NpssoError):
        parse_npsso(json.dumps({"npsso": ""}))
