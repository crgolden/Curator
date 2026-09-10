"""Tests for JwtValidator: a locally generated RSA key signs tokens with joserfc, and a fake ``fetch_json``
serves the discovery document + JWKS -- no network access, no real Identity instance.

The second half covers the other outcome: Identity being unreachable, which is
:class:`AuthorityUnavailableError` and not a :class:`TokenError`. Both halves are needed together --
proving the outage path raises the new type is worth nothing without the existing tests proving a genuinely
bad token still raises the old one."""

from __future__ import annotations

import json
import time
import urllib.error

import pytest
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey

from curator.token_validation import AuthorityUnavailableError, JwtValidator, TokenError, _parse_scopes

AUTHORITY = "https://identity.example.test"
DISCOVERY_URL = f"{AUTHORITY}/.well-known/openid-configuration"
JWKS_URL = f"{AUTHORITY}/.well-known/jwks"


def _generate_key(kid: str) -> RSAKey:
    return RSAKey.generate_key(2048, {"kid": kid, "use": "sig", "alg": "RS256"}, private=True)


def _sign(key: RSAKey, kid: str, **payload_overrides: object) -> str:
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
    return jwt.encode(header, payload, key)


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
    jwks = {"keys": [key.as_dict(private=False)]}
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
    impostor_key_with_the_same_kid = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(impostor_key_with_the_same_kid, "key-1")

    with pytest.raises(TokenError):
        validator.validate(token)


def test_wrong_issuer_is_rejected():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(key, "key-1", iss="https://evil.example.test")

    with pytest.raises(TokenError):
        validator.validate(token)


def test_expired_token_is_rejected():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    now = int(time.time())
    token = _sign(key, "key-1", iat=now - 7200, exp=now - 3600)

    with pytest.raises(TokenError):
        validator.validate(token)


def test_scope_as_space_delimited_string_is_accepted():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(key, "key-1", scope="curator openid")

    claims = validator.validate(token)

    assert claims.scopes == ("curator", "openid")


def test_scope_as_json_array_is_accepted():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(key, "key-1", scope=["curator"])

    claims = validator.validate(token)

    assert claims.scopes == ("curator",)


def test_missing_sub_is_rejected():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    now = int(time.time())
    header = {"alg": "RS256", "kid": "key-1"}
    payload = {"iss": AUTHORITY, "email": "user@example.test", "iat": now, "exp": now + 3600}
    token = jwt.encode(header, payload, key)

    with pytest.raises(TokenError):
        validator.validate(token)


def test_missing_email_claim_yields_none_email():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    now = int(time.time())
    header = {"alg": "RS256", "kid": "key-1"}
    payload = {"iss": AUTHORITY, "sub": "sub-1", "scope": ["curator"], "iat": now, "exp": now + 3600}
    token = jwt.encode(header, payload, key)

    claims = validator.validate(token)

    assert claims.email is None


def test_unknown_kid_triggers_a_refetch_and_succeeds_once_the_key_is_present():
    old_key = _generate_key("key-1")
    new_key = _generate_key("key-2")
    fetcher = FakeFetcher({"keys": [old_key.as_dict(private=False)]})
    validator = JwtValidator(AUTHORITY, fetch_json=fetcher)

    token_old = _sign(old_key, "key-1")
    validator.validate(token_old)
    fetch_count_after_first_validate = len(fetcher.urls)

    fetcher.jwks = {"keys": [old_key.as_dict(private=False), new_key.as_dict(private=False)]}
    token_new = _sign(new_key, "key-2")

    claims = validator.validate(token_new)

    assert claims.sub == "sub-1"
    assert len(fetcher.urls) > fetch_count_after_first_validate


def test_unknown_kid_still_rejected_after_refetch_if_truly_absent():
    key = _generate_key("key-1")
    jwks = {"keys": [key.as_dict(private=False)]}
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


_IDENTITY_RETURNED_ITS_OWN_500 = 500


class FailingFetcher:
    """A ``fetch_json`` that fails on every call, the way the real one does when Identity is down."""

    def __init__(self, failure: Exception):
        self.failure = failure

    def __call__(self, url: str) -> dict:
        raise self.failure


class KeysetFailsAfterTheFirstFetcher(FakeFetcher):
    """Serves the JWKS once and then goes down, so the ``force=True`` refetch ``_decode`` performs for an
    unrecognized ``kid`` is the call that meets the outage."""

    def __init__(self, jwks: dict):
        super().__init__(jwks)
        self.keysets_served = 0

    def __call__(self, url: str) -> dict:
        if url == JWKS_URL:
            self.keysets_served += 1
            if self.keysets_served > 1:
                raise urllib.error.HTTPError(url, _IDENTITY_RETURNED_ITS_OWN_500, "Internal Server Error", {}, None)
        return super().__call__(url)


def _validate_any_token(fetch_json) -> None:
    """Drive a validation far enough to force a keyset fetch. Which token is irrelevant -- every case here
    fails before a token is ever examined, which is the property under test."""
    key = _generate_key("key-1")
    JwtValidator(AUTHORITY, fetch_json=fetch_json).validate(_sign(key, "key-1"))


@pytest.mark.parametrize(
    ("shape", "failure"),
    [
        (
            "identity answered with its own 500",
            urllib.error.HTTPError(DISCOVERY_URL, _IDENTITY_RETURNED_ITS_OWN_500, "Internal Server Error", {}, None),
        ),
        ("identity was unreachable", urllib.error.URLError("connection refused")),
        ("the fetch timed out", TimeoutError("timed out")),
        ("identity answered with something that is not json", json.JSONDecodeError("Expecting value", "<html/>", 0)),
    ],
)
def test_a_failed_discovery_fetch_is_an_authority_outage_not_a_bad_token(shape, failure):
    with pytest.raises(AuthorityUnavailableError):
        _validate_any_token(FailingFetcher(failure))


def test_a_discovery_document_carrying_no_jwks_uri_is_an_authority_outage():
    """A 200 whose body is JSON but is not a discovery document. Reads as ``KeyError`` at the subscript,
    which says nothing about the caller's token either."""

    def serve_a_body_that_is_not_a_discovery_document(url: str) -> dict:
        return {}

    with pytest.raises(AuthorityUnavailableError):
        _validate_any_token(serve_a_body_that_is_not_a_discovery_document)


def test_a_jwks_joserfc_cannot_import_is_an_authority_outage():
    """The one failure raised *outside* the two fetches but still inside ``_ensure_keyset``. Wrapping only
    the fetches would leave this escaping as a bare 500, which is the hole this whole change closes."""
    unimportable_jwks = {"keys": [{"kty": "not-a-real-key-type"}]}
    fetcher = FakeFetcher(unimportable_jwks)

    with pytest.raises(AuthorityUnavailableError):
        _validate_any_token(fetcher)


def test_identity_going_down_between_the_cached_keyset_and_the_kid_refetch_is_an_authority_outage():
    """``_decode`` catches ``JoseError`` around both ``jwt.decode`` calls and performs the ``force=True``
    refetch between them. An outage during that refetch must travel straight through, not be re-read as an
    unverifiable token."""
    original_key = _generate_key("key-1")
    fetcher = KeysetFailsAfterTheFirstFetcher({"keys": [original_key.as_dict(private=False)]})
    validator = JwtValidator(AUTHORITY, fetch_json=fetcher)
    validator.validate(_sign(original_key, "key-1"))

    rotated_key = _generate_key("key-2")
    token_signed_with_a_kid_the_cache_predates = _sign(rotated_key, "key-2")

    with pytest.raises(AuthorityUnavailableError):
        validator.validate(token_signed_with_a_kid_the_cache_predates)

    assert fetcher.keysets_served > 1, "the outage must have fallen on the refetch, not on the first fetch"


def test_an_authority_outage_is_not_a_token_error_and_not_a_jose_error():
    """The class relationships are the whole mechanism. ``curator.deps.require_bearer`` tells the two
    apart by type to answer 503 rather than 401, and ``_decode``'s ``except JoseError`` would swallow the
    outage back into a 401 if it ever became one -- so pin both, because a later refactor that changes
    either base class would silently undo this with every test above still green."""
    assert not issubclass(AuthorityUnavailableError, TokenError)
    assert not issubclass(AuthorityUnavailableError, JoseError)
    assert not issubclass(TokenError, AuthorityUnavailableError)
