"""Tests for JwtValidator: a locally generated RSA key signs tokens with Authlib, and a fake ``fetch_json``
serves the discovery document + JWKS -- no network access, no real Identity instance."""

from __future__ import annotations

import time

import pytest
from authlib.jose import JsonWebKey, JsonWebToken

from curator.token_validation import JwtValidator, TokenError, _parse_scopes

AUTHORITY = "https://identity.example.test"
DISCOVERY_URL = f"{AUTHORITY}/.well-known/openid-configuration"
JWKS_URL = f"{AUTHORITY}/.well-known/jwks"

_jwt = JsonWebToken(["RS256"])


def _generate_key(kid: str):
    return JsonWebKey.generate_key("RSA", 2048, {"kid": kid, "use": "sig", "alg": "RS256"}, is_private=True)


def _sign(key, kid: str, **payload_overrides) -> str:
    now = int(time.time())
    payload = {
        "iss": AUTHORITY,
        "sub": "sub-1",
        "email": "user@example.test",
        "scope": ["curator", "openid"],
        "iat": now,
        "exp": now + 3600,
    }
    payload.update(payload_overrides)
    header = {"alg": "RS256", "kid": kid}
    # authlib ships no type stubs, so `.encode(...)` resolves as Any; `str(...)` narrows it back to the
    # `str` this helper declares, with no behavioral change (the value is already a decoded ascii str).
    return str(_jwt.encode(header, payload, key).decode("ascii"))


class FakeFetcher:
    """Serves canned discovery/JWKS documents; records every URL fetched."""

    def __init__(self, jwks: dict):
        self.jwks = jwks
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.urls.append(url)
        if url == DISCOVERY_URL:
            return {"jwks_uri": JWKS_URL}
        if url == JWKS_URL:
            return self.jwks
        raise AssertionError(f"unexpected fetch: {url}")


def _make_validator(jwks: dict) -> tuple[JwtValidator, FakeFetcher]:
    fetcher = FakeFetcher(jwks)
    return JwtValidator(AUTHORITY, fetch_json=fetcher), fetcher


def test_valid_token_is_accepted_and_claims_extracted():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(is_private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(key, "key-1")

    claims = validator.validate(token)

    assert claims.sub == "sub-1"
    assert claims.email == "user@example.test"
    assert claims.scopes == ("curator", "openid")
    assert claims.has_scope("curator") is True
    assert claims.iat.tzinfo is not None


def test_wrong_signature_is_rejected():
    key = _generate_key("key-1")
    other_key = _generate_key("key-1")  # same kid, different key material
    jwks = {"keys": [key.as_dict(is_private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(other_key, "key-1")

    with pytest.raises(TokenError):
        validator.validate(token)


def test_wrong_issuer_is_rejected():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(is_private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(key, "key-1", iss="https://evil.example.test")

    with pytest.raises(TokenError):
        validator.validate(token)


def test_expired_token_is_rejected():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(is_private=False)]}
    validator, _fetcher = _make_validator(jwks)
    now = int(time.time())
    token = _sign(key, "key-1", iat=now - 7200, exp=now - 3600)

    with pytest.raises(TokenError):
        validator.validate(token)


def test_scope_as_space_delimited_string_is_accepted():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(is_private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(key, "key-1", scope="curator openid")

    claims = validator.validate(token)

    assert claims.scopes == ("curator", "openid")


def test_scope_as_json_array_is_accepted():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(is_private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(key, "key-1", scope=["curator"])

    claims = validator.validate(token)

    assert claims.scopes == ("curator",)


def test_missing_sub_is_rejected():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(is_private=False)]}
    validator, _fetcher = _make_validator(jwks)
    now = int(time.time())
    header = {"alg": "RS256", "kid": "key-1"}
    payload = {"iss": AUTHORITY, "email": "user@example.test", "iat": now, "exp": now + 3600}
    token = _jwt.encode(header, payload, key).decode("ascii")

    with pytest.raises(TokenError):
        validator.validate(token)


def test_missing_email_claim_yields_none_email():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(is_private=False)]}
    validator, _fetcher = _make_validator(jwks)
    now = int(time.time())
    header = {"alg": "RS256", "kid": "key-1"}
    payload = {"iss": AUTHORITY, "sub": "sub-1", "scope": ["curator"], "iat": now, "exp": now + 3600}
    token = _jwt.encode(header, payload, key).decode("ascii")

    claims = validator.validate(token)

    assert claims.email is None


def test_unknown_kid_triggers_a_refetch_and_succeeds_once_the_key_is_present():
    old_key = _generate_key("key-1")
    new_key = _generate_key("key-2")
    fetcher = FakeFetcher({"keys": [old_key.as_dict(is_private=False)]})
    validator = JwtValidator(AUTHORITY, fetch_json=fetcher)

    # Prime the validator's cache with the "old" JWKS (simulating a Curator process that started before
    # Identity rotated in key-2).
    token_old = _sign(old_key, "key-1")
    validator.validate(token_old)
    fetch_count_after_first_validate = len(fetcher.urls)

    # Identity rotates: key-2 appears in the JWKS. A token signed with it carries an unrecognized kid, so
    # the validator must refetch (not just fail) before giving up.
    fetcher.jwks = {"keys": [old_key.as_dict(is_private=False), new_key.as_dict(is_private=False)]}
    token_new = _sign(new_key, "key-2")

    claims = validator.validate(token_new)

    assert claims.sub == "sub-1"
    assert len(fetcher.urls) > fetch_count_after_first_validate


def test_unknown_kid_still_rejected_after_refetch_if_truly_absent():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(is_private=False)]}
    validator, _fetcher = _make_validator(jwks)
    other_key = _generate_key("key-999")
    token = _sign(other_key, "key-999")

    with pytest.raises(TokenError):
        validator.validate(token)


def test_parse_scopes_handles_list_string_and_none():
    assert _parse_scopes(["a", "b"]) == ("a", "b")
    assert _parse_scopes("a b") == ("a", "b")
    assert _parse_scopes(None) == ()
    assert _parse_scopes("") == ()
