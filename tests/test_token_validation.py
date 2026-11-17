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
from http import HTTPStatus

import pytest
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey

from curator.token_validation import (
    _ALGORITHMS,
    EMAIL_CLAIM,
    IAT_CLAIM,
    JWKS_URI_KEY,
    SCOPE_CLAIM,
    SUB_CLAIM,
    AuthorityUnavailableError,
    JwtValidator,
    TokenError,
    _parse_scopes,
    discovery_url,
)
from test_values import (
    lowercase_token,
    new_access_token_lifetime_seconds,
    new_email_address,
    new_endpoint_url,
    new_identity_sub,
    new_positive_count,
)

AUTHORITY = new_endpoint_url()
DISCOVERY_URL = discovery_url(AUTHORITY)
JWKS_URL = f"{AUTHORITY}/{lowercase_token()}"
SUB = new_identity_sub()
EMAIL = new_email_address()
SCOPES = (lowercase_token(), lowercase_token())
_SIGNING_ALGORITHM = _ALGORITHMS[0]


def _generate_key(kid: str) -> RSAKey:
    return RSAKey.generate_key(2048, {"kid": kid, "use": "sig", "alg": _SIGNING_ALGORITHM}, private=True)


def _sign(key: RSAKey, kid: str, **payload_overrides: object) -> str:
    now = int(time.time())
    payload = {
        "iss": AUTHORITY,
        SUB_CLAIM: SUB,
        EMAIL_CLAIM: EMAIL,
        SCOPE_CLAIM: list(SCOPES),
        IAT_CLAIM: now,
        "exp": now + new_access_token_lifetime_seconds(),
    }
    payload.update(payload_overrides)
    header = {"alg": _SIGNING_ALGORITHM, "kid": kid}
    return jwt.encode(header, payload, key)


class FakeFetcher:
    """Serves canned discovery/JWKS documents; records every URL fetched."""

    def __init__(self, jwks: dict):
        self.jwks = jwks
        self.urls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.urls.append(url)
        if url == DISCOVERY_URL:
            return {JWKS_URI_KEY: JWKS_URL}
        if url == JWKS_URL:
            return self.jwks
        raise AssertionError(f"unexpected fetch: {url}")


def _make_validator(jwks: dict) -> tuple[JwtValidator, FakeFetcher]:
    fetcher = FakeFetcher(jwks)
    return JwtValidator(AUTHORITY, fetch_json=fetcher), fetcher


def test_valid_token_is_accepted_and_claims_extracted():
    kid = lowercase_token()
    key = _generate_key(kid)
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(key, kid)

    claims = validator.validate(token)

    assert claims.sub == SUB
    assert claims.email == EMAIL
    assert claims.scopes == SCOPES
    assert claims.has_scope(SCOPES[0]) is True
    assert claims.iat.tzinfo is not None


def test_wrong_signature_is_rejected():
    kid = lowercase_token()
    key = _generate_key(kid)
    impostor_key_with_the_same_kid = _generate_key(kid)
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(impostor_key_with_the_same_kid, kid)

    with pytest.raises(TokenError):
        validator.validate(token)


def test_wrong_issuer_is_rejected():
    kid = lowercase_token()
    key = _generate_key(kid)
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(key, kid, iss=new_endpoint_url())

    with pytest.raises(TokenError):
        validator.validate(token)


def test_expired_token_is_rejected():
    kid = lowercase_token()
    key = _generate_key(kid)
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    expired_at = int(time.time()) - new_positive_count()
    issued_at = expired_at - new_access_token_lifetime_seconds()
    token = _sign(key, kid, iat=issued_at, exp=expired_at)

    with pytest.raises(TokenError):
        validator.validate(token)


def test_scope_as_space_delimited_string_is_accepted():
    kid = lowercase_token()
    key = _generate_key(kid)
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(key, kid, **{SCOPE_CLAIM: " ".join(SCOPES)})

    claims = validator.validate(token)

    assert claims.scopes == SCOPES


def test_scope_as_json_array_is_accepted():
    kid = lowercase_token()
    key = _generate_key(kid)
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    token = _sign(key, kid, **{SCOPE_CLAIM: [SCOPES[0]]})

    claims = validator.validate(token)

    assert claims.scopes == (SCOPES[0],)


def test_missing_sub_is_rejected():
    kid = lowercase_token()
    key = _generate_key(kid)
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    now = int(time.time())
    header = {"alg": _SIGNING_ALGORITHM, "kid": kid}
    payload = {"iss": AUTHORITY, EMAIL_CLAIM: EMAIL, IAT_CLAIM: now, "exp": now + new_access_token_lifetime_seconds()}
    token = jwt.encode(header, payload, key)

    with pytest.raises(TokenError):
        validator.validate(token)


def test_missing_email_claim_yields_none_email():
    kid = lowercase_token()
    key = _generate_key(kid)
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    now = int(time.time())
    header = {"alg": _SIGNING_ALGORITHM, "kid": kid}
    payload = {
        "iss": AUTHORITY,
        SUB_CLAIM: SUB,
        SCOPE_CLAIM: list(SCOPES),
        IAT_CLAIM: now,
        "exp": now + new_access_token_lifetime_seconds(),
    }
    token = jwt.encode(header, payload, key)

    claims = validator.validate(token)

    assert claims.email is None


def test_unknown_kid_triggers_a_refetch_and_succeeds_once_the_key_is_present():
    old_kid, new_kid = lowercase_token(), lowercase_token()
    old_key = _generate_key(old_kid)
    new_key = _generate_key(new_kid)
    fetcher = FakeFetcher({"keys": [old_key.as_dict(private=False)]})
    validator = JwtValidator(AUTHORITY, fetch_json=fetcher)

    token_old = _sign(old_key, old_kid)
    validator.validate(token_old)
    fetch_count_after_first_validate = len(fetcher.urls)

    fetcher.jwks = {"keys": [old_key.as_dict(private=False), new_key.as_dict(private=False)]}
    token_new = _sign(new_key, new_kid)

    claims = validator.validate(token_new)

    assert claims.sub == SUB
    assert fetcher.urls[fetch_count_after_first_validate:] == [DISCOVERY_URL, JWKS_URL]


def test_unknown_kid_still_rejected_after_refetch_if_truly_absent():
    key = _generate_key(lowercase_token())
    jwks = {"keys": [key.as_dict(private=False)]}
    validator, _fetcher = _make_validator(jwks)
    other_kid = lowercase_token()
    other_key = _generate_key(other_kid)
    token = _sign(other_key, other_kid)

    with pytest.raises(TokenError):
        validator.validate(token)


def test_parse_scopes_handles_list_string_and_none():
    first, second = SCOPES
    assert _parse_scopes([first, second]) == SCOPES
    assert _parse_scopes(f"{first} {second}") == SCOPES
    assert _parse_scopes(None) == ()
    assert _parse_scopes("") == ()


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
                raise urllib.error.HTTPError(url, HTTPStatus.INTERNAL_SERVER_ERROR, lowercase_token(), {}, None)
        return super().__call__(url)


def _validate_a_token_that_would_otherwise_be_valid(fetch_json) -> None:
    kid = lowercase_token()
    key = _generate_key(kid)
    JwtValidator(AUTHORITY, fetch_json=fetch_json).validate(_sign(key, kid))


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param(
            urllib.error.HTTPError(DISCOVERY_URL, HTTPStatus.INTERNAL_SERVER_ERROR, lowercase_token(), {}, None),
            id="identity answered with its own 500",
        ),
        pytest.param(urllib.error.URLError(lowercase_token()), id="identity was unreachable"),
        pytest.param(TimeoutError(lowercase_token()), id="the fetch timed out"),
        pytest.param(
            json.JSONDecodeError(lowercase_token(), lowercase_token(), 0),
            id="identity answered with something that is not json",
        ),
    ],
)
def test_a_failed_discovery_fetch_is_an_authority_outage_not_a_bad_token(failure):
    fetcher = FailingFetcher(failure)

    with pytest.raises(AuthorityUnavailableError):
        _validate_a_token_that_would_otherwise_be_valid(fetcher)


def test_a_discovery_document_carrying_no_jwks_uri_is_an_authority_outage():
    def serve_a_body_that_is_not_a_discovery_document(url: str) -> dict:
        return {}

    with pytest.raises(AuthorityUnavailableError):
        _validate_a_token_that_would_otherwise_be_valid(serve_a_body_that_is_not_a_discovery_document)


def test_a_jwks_joserfc_cannot_import_is_an_authority_outage_though_no_fetch_failed():
    unimportable_jwks = {"keys": [{"kty": lowercase_token()}]}
    fetcher = FakeFetcher(unimportable_jwks)

    with pytest.raises(AuthorityUnavailableError):
        _validate_a_token_that_would_otherwise_be_valid(fetcher)


def test_an_outage_during_the_kid_refetch_is_an_authority_outage_not_an_unverifiable_token():
    original_kid = lowercase_token()
    original_key = _generate_key(original_kid)
    fetcher = KeysetFailsAfterTheFirstFetcher({"keys": [original_key.as_dict(private=False)]})
    validator = JwtValidator(AUTHORITY, fetch_json=fetcher)
    validator.validate(_sign(original_key, original_kid))

    rotated_kid = lowercase_token()
    rotated_key = _generate_key(rotated_kid)
    token_signed_with_a_kid_the_cache_predates = _sign(rotated_key, rotated_kid)

    with pytest.raises(AuthorityUnavailableError):
        validator.validate(token_signed_with_a_kid_the_cache_predates)

    assert fetcher.keysets_served > 1, "the outage must have fallen on the refetch, not on the first fetch"


def test_an_authority_outage_is_not_a_token_error_and_not_a_jose_error():
    assert not issubclass(AuthorityUnavailableError, TokenError)
    assert not issubclass(AuthorityUnavailableError, JoseError)
    assert not issubclass(TokenError, AuthorityUnavailableError)
