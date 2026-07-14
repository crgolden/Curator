"""Tests for parse_npsso(), ported from psnpy's test_config.py -- only the parse_npsso subset. The
resolve_npsso()/npsso_env_names() env-var resolution chain is intentionally NOT ported: Curator always
receives the npsso per-request via POST /psn/link's body, so there is no "resolve from environment"
concept to preserve (see the migration plan's psnpy capability audit)."""

from __future__ import annotations

import pytest

from curator.psn.npsso import NpssoError, parse_npsso


def test_parse_npsso_raw_token_passthrough():
    token = "abcdef0123456789" * 4
    assert parse_npsso(token) == token


def test_parse_npsso_unwraps_json_blob():
    assert parse_npsso('{"npsso": "the-token"}') == "the-token"


def test_parse_npsso_malformed_json_raises():
    with pytest.raises(NpssoError):
        parse_npsso('{"npsso":')


def test_parse_npsso_missing_key_raises():
    with pytest.raises(NpssoError):
        parse_npsso('{"other": "value"}')


def test_parse_npsso_strips_whitespace():
    assert parse_npsso("  raw-token  ") == "raw-token"


def test_parse_npsso_empty_npsso_value_raises():
    with pytest.raises(NpssoError):
        parse_npsso('{"npsso": ""}')
