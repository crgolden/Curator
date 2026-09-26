"""Tests for PsnSession's async auth bootstrap, refresh, and request plumbing.

Ported from ``psnpy``'s ``test_psn_api.py``, rewritten async against ``httpx.MockTransport`` (the async
equivalent of that suite's ``requests.Session.request`` monkeypatch) instead of a real network call, with a
hand-written fake token store and rate limiter -- no ``unittest.mock`` anywhere, matching this repo's
testing convention.
"""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import pytest

from curator.http_headers import (
    AUTHORIZATION_HEADER,
    BEARER_SCHEME,
    COOKIE_HEADER,
    DELETE_METHOD,
    GET_METHOD,
    LOCATION_HEADER,
    PATCH_METHOD,
    POST_METHOD,
    PUT_METHOD,
)
from curator.psn._graphql import GRAPHQL_URL
from curator.psn._identity import LEGACY_PROFILE_URI, MY_ACCOUNT_URL, PROFILE_URI
from curator.psn.account_client import ACCOUNT_ME_URL
from curator.psn.errors import PsnAuthError
from curator.psn.presence_client import PROFILE_URI_V2
from curator.psn.session import (
    ACCESS_TYPE_PARAM,
    AUTHORIZE_URL,
    CODE_PARAM,
    ERROR_PARAM,
    GRANT_TYPE_PARAM,
    OFFLINE_ACCESS_TYPE,
    READ_ATTEMPTS,
    REDIRECT_URI,
    REFRESH_TOKEN_GRANT,
    TOKEN_URL,
    PsnSession,
    is_transient_psn_failure,
    npsso_cookie,
)
from curator.psn.social_client import CPSS_URI, GAMING_LOUNGE_URI
from curator.psn.store_client import STORE_GRAPHQL_URL
from curator.psn.trophy_client import GAMES_LIST_URI, TROPHIES_URI
from curator.token_response import (
    ACCESS_TOKEN_EXPIRES_AT_KEY,
    ACCESS_TOKEN_KEY,
    EXPIRES_IN_KEY,
    REFRESH_TOKEN_EXPIRES_AT_KEY,
    REFRESH_TOKEN_EXPIRES_IN_KEY,
    REFRESH_TOKEN_KEY,
    SCOPE_KEY,
    TOKEN_TYPE_KEY,
)
from test_values import lowercase_token, new_opaque_token, new_positive_count

_ONE_HOUR_SECONDS = 3600


@pytest.fixture
def no_retry_backoff(monkeypatch):
    """Collapse the retry backoff so a test that exhausts attempts doesn't spend seconds sleeping.

    ``_request`` reads the multiplier at call time, so patching the module attribute is enough.
    """
    monkeypatch.setattr("curator.psn.session.RETRY_BACKOFF_MULTIPLIER_SECONDS", 0)
    monkeypatch.setattr("curator.psn.session.RETRY_MAX_WAIT_SECONDS", 0)


class FakeTokenStore:
    def __init__(self, saved=None):
        self._saved = saved
        self.saved_calls: list[dict] = []

    async def load(self):
        return self._saved

    async def save(self, token_response):
        self.saved_calls.append(token_response)
        self._saved = token_response

    async def clear(self):
        self._saved = None


class FakeRateLimiter:
    def __init__(self):
        self.acquire_calls = 0

    async def acquire(self):
        self.acquire_calls += 1


def _token_response(**overrides):
    body = {
        ACCESS_TOKEN_KEY: new_opaque_token(),
        REFRESH_TOKEN_KEY: new_opaque_token(),
        TOKEN_TYPE_KEY: lowercase_token(),
        EXPIRES_IN_KEY: _ONE_HOUR_SECONDS,
        SCOPE_KEY: lowercase_token(),
        REFRESH_TOKEN_EXPIRES_IN_KEY: new_positive_count() * _ONE_HOUR_SECONDS,
    }
    body.update(overrides)
    return body


def _live_token_response(**overrides):
    return _token_response(**{ACCESS_TOKEN_EXPIRES_AT_KEY: time.time() + _ONE_HOUR_SECONDS, **overrides})


def _redirect(**query):
    """The authorization endpoint's answer: a redirect to the app's own scheme carrying ``query``."""
    return httpx.Response(httpx.codes.FOUND, headers={LOCATION_HEADER: f"{REDIRECT_URI}?{urlencode(query)}"})


def _psn_url():
    return f"{PROFILE_URI}/{lowercase_token()}"


def _form(request):
    return {key: values[0] for key, values in parse_qs(request.content.decode()).items()}


class RequestRecorder:
    """Records every request an ``httpx.MockTransport`` receives and returns a queued response per call."""

    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)


def _session(recorder, *, npsso=None, token_store=None, rate_limiter=None) -> PsnSession:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return PsnSession(npsso, token_store=token_store, rate_limiter=rate_limiter or FakeRateLimiter(), client=client)


async def _restored(recorder, saved, *, rate_limiter=None) -> PsnSession:
    store = FakeTokenStore(saved=saved)
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return await PsnSession.restore(None, store, rate_limiter=rate_limiter, client=client)


async def _authenticated(recorder) -> PsnSession:
    return await _restored(recorder, _live_token_response())


async def test_bootstrap_from_npsso_sets_token():
    npsso = new_opaque_token()
    issued = _token_response()
    recorder = RequestRecorder([_redirect(**{CODE_PARAM: new_opaque_token()}), httpx.Response(200, json=issued)])
    session = _session(recorder, npsso=npsso)

    await session._ensure_fresh()

    assert session.token_response is not None
    assert session.token_response[ACCESS_TOKEN_KEY] == issued[ACCESS_TOKEN_KEY]
    assert session.token_response[ACCESS_TOKEN_EXPIRES_AT_KEY] > time.time()
    authorize_request, token_request = recorder.requests
    authorize_endpoint = urlparse(str(authorize_request.url))._replace(query="").geturl()
    assert (authorize_request.method, authorize_endpoint) == (GET_METHOD, AUTHORIZE_URL)
    assert authorize_request.headers[COOKIE_HEADER] == npsso_cookie(npsso)
    assert (token_request.method, str(token_request.url)) == (POST_METHOD, TOKEN_URL)


async def test_bootstrap_requests_offline_access():
    """access_type=offline is what makes PSN issue a refresh_token at all -- a real production bug once had
    this missing, silently downgrading every user's link to an access-token-only session that expired in
    ~1 hour. The pin locks the value; the second assert proves the request carries it."""
    issued = _token_response()
    recorder = RequestRecorder([_redirect(**{CODE_PARAM: new_opaque_token()}), httpx.Response(200, json=issued)])
    session = _session(recorder, npsso=new_opaque_token())

    await session._ensure_fresh()

    assert OFFLINE_ACCESS_TYPE == "offline"
    assert recorder.requests[0].url.params[ACCESS_TYPE_PARAM] == OFFLINE_ACCESS_TYPE


async def test_bootstrap_handles_response_without_refresh_token():
    issued = _token_response()
    del issued[REFRESH_TOKEN_KEY]
    del issued[REFRESH_TOKEN_EXPIRES_IN_KEY]
    recorder = RequestRecorder([_redirect(**{CODE_PARAM: new_opaque_token()}), httpx.Response(200, json=issued)])
    session = _session(recorder, npsso=new_opaque_token())

    await session._ensure_fresh()

    assert session.token_response is not None
    assert session.token_response[ACCESS_TOKEN_KEY] == issued[ACCESS_TOKEN_KEY]
    assert REFRESH_TOKEN_EXPIRES_AT_KEY not in session.token_response


async def test_bootstrap_raises_on_expired_npsso():
    recorder = RequestRecorder([_redirect(**{ERROR_PARAM: lowercase_token()})])
    session = _session(recorder, npsso=new_opaque_token())

    with pytest.raises(PsnAuthError, match="expired or is incorrect"):
        await session._ensure_fresh()


async def test_restore_uses_cached_token_without_bootstrapping():
    cached = _live_token_response()
    recorder = RequestRecorder([])
    session = await _restored(recorder, cached)

    await session._ensure_fresh()

    assert recorder.requests == []
    assert session.token_response == cached


async def test_expired_cached_token_triggers_refresh_and_persists_it():
    refreshed = _token_response()
    expired = _token_response(**{ACCESS_TOKEN_EXPIRES_AT_KEY: time.time() - 10})
    recorder = RequestRecorder([httpx.Response(200, json=refreshed)])
    store = FakeTokenStore(saved=expired)
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    session = await PsnSession.restore(None, store, client=client)

    await session._ensure_fresh()

    assert session.token_response is not None
    assert session.token_response[ACCESS_TOKEN_KEY] == refreshed[ACCESS_TOKEN_KEY]
    form = _form(recorder.requests[0])
    assert form[GRANT_TYPE_PARAM] == REFRESH_TOKEN_GRANT
    assert form[REFRESH_TOKEN_KEY] == expired[REFRESH_TOKEN_KEY]
    assert store.saved_calls[-1][ACCESS_TOKEN_KEY] == refreshed[ACCESS_TOKEN_KEY]


async def test_restore_requires_npsso_or_cached_token():
    with pytest.raises(ValueError, match="No cached token and no npsso"):
        await PsnSession.restore(None, FakeTokenStore(saved=None))


async def test_get_attaches_bearer_and_ensures_fresh_token():
    body = {lowercase_token(): lowercase_token()}
    cached = _live_token_response()
    recorder = RequestRecorder([httpx.Response(200, json=body)])
    session = await _restored(recorder, cached)
    url, params = _psn_url(), {lowercase_token(): lowercase_token()}

    response = await session.get(url, params=params)

    assert response.json() == body
    request = recorder.requests[0]
    assert request.method == GET_METHOD
    assert str(request.url) == f"{url}?{urlencode(params)}"
    assert request.headers[AUTHORIZATION_HEADER] == f"{BEARER_SCHEME} {cached[ACCESS_TOKEN_KEY]}"


async def test_post_raises_psn_auth_error_on_401():
    recorder = RequestRecorder([httpx.Response(401)])
    session = await _authenticated(recorder)

    with pytest.raises(PsnAuthError, match="401"):
        await session.post(_psn_url(), json={lowercase_token(): lowercase_token()})


async def test_patch_put_delete_attach_bearer():
    cached = _live_token_response()
    recorder = RequestRecorder([httpx.Response(200, json={}), httpx.Response(200), httpx.Response(200)])
    session = await _restored(recorder, cached)

    await session.patch(_psn_url(), json={lowercase_token(): lowercase_token()})
    await session.put(_psn_url())
    await session.delete(_psn_url())

    assert [(r.method, r.headers[AUTHORIZATION_HEADER]) for r in recorder.requests] == [
        (PATCH_METHOD, f"{BEARER_SCHEME} {cached[ACCESS_TOKEN_KEY]}"),
        (PUT_METHOD, f"{BEARER_SCHEME} {cached[ACCESS_TOKEN_KEY]}"),
        (DELETE_METHOD, f"{BEARER_SCHEME} {cached[ACCESS_TOKEN_KEY]}"),
    ]


async def test_get_raises_for_other_http_errors(no_retry_backoff):
    recorder = RequestRecorder([httpx.Response(500)] * READ_ATTEMPTS)
    session = await _authenticated(recorder)

    with pytest.raises(httpx.HTTPStatusError):
        await session.get(_psn_url())


def _foreign_host_url():
    return f"https://{lowercase_token()}.invalid/{lowercase_token()}"


def _psn_host_suffixed_url():
    return f"https://{urlparse(PROFILE_URI).hostname}.{lowercase_token()}.invalid/{lowercase_token()}"


def _plain_http_psn_url():
    return urlparse(_psn_url())._replace(scheme="http").geturl()


@pytest.mark.parametrize("make_url", [_foreign_host_url, _psn_host_suffixed_url, _plain_http_psn_url])
async def test_a_request_to_anything_but_a_psn_host_over_https_is_refused(make_url):
    recorder = RequestRecorder([httpx.Response(200)])
    session = await _authenticated(recorder)

    with pytest.raises(ValueError, match="non-PSN URL"):
        await session.get(make_url())

    assert recorder.requests == []


async def test_a_path_that_escapes_its_endpoint_is_refused():
    recorder = RequestRecorder([httpx.Response(200)])
    session = await _authenticated(recorder)

    with pytest.raises(ValueError, match="traversal segment"):
        await session.get(f"{GAMING_LOUNGE_URI}/../../{lowercase_token()}")

    assert recorder.requests == []


@pytest.mark.parametrize(
    "base_url",
    [
        AUTHORIZE_URL,
        TOKEN_URL,
        PROFILE_URI,
        PROFILE_URI_V2,
        LEGACY_PROFILE_URI,
        MY_ACCOUNT_URL,
        ACCOUNT_ME_URL,
        GRAPHQL_URL,
        STORE_GRAPHQL_URL,
        TROPHIES_URI,
        GAMES_LIST_URI,
        GAMING_LOUNGE_URI,
        CPSS_URI,
    ],
)
def test_every_base_url_a_psn_client_calls_is_allowed(base_url):
    """Parametrized over the clients' own base URLs rather than over the allowlist, so a client that moves
    to a host the allowlist does not name fails here instead of in production."""
    assert PsnSession._verified_url(base_url) == base_url


async def test_every_request_acquires_the_rate_limiter():
    rate_limiter = FakeRateLimiter()
    session = await _restored(RequestRecorder([httpx.Response(200)]), _live_token_response(), rate_limiter=rate_limiter)

    await session.get(_psn_url())

    assert rate_limiter.acquire_calls == 1


async def test_run_with_reauth_refreshes_once_then_retries_when_there_is_no_npsso():
    refreshed = _token_response()
    retried_body = {lowercase_token(): lowercase_token()}
    recorder = RequestRecorder(
        [httpx.Response(401), httpx.Response(200, json=refreshed), httpx.Response(200, json=retried_body)]
    )
    session = _session(recorder)
    session.token_response = _live_token_response()
    url = _psn_url()

    async def operation():
        return await session.get(url)

    response = await session.run_with_reauth(operation)

    assert response.json() == retried_body
    assert session.token_response is not None
    assert session.token_response[ACCESS_TOKEN_KEY] == refreshed[ACCESS_TOKEN_KEY]
    assert str(recorder.requests[1].url) == TOKEN_URL


async def test_run_with_reauth_bootstraps_from_npsso_when_there_is_no_refresh_token():
    rebootstrapped = _token_response()
    retried_body = {lowercase_token(): lowercase_token()}
    recorder = RequestRecorder(
        [
            httpx.Response(401),
            _redirect(**{CODE_PARAM: new_opaque_token()}),
            httpx.Response(200, json=rebootstrapped),
            httpx.Response(200, json=retried_body),
        ]
    )
    session = _session(recorder, npsso=new_opaque_token())
    session.token_response = _live_token_response(**{REFRESH_TOKEN_KEY: None})
    url = _psn_url()

    async def operation():
        return await session.get(url)

    response = await session.run_with_reauth(operation)

    assert response.json() == retried_body
    assert session.token_response is not None
    assert session.token_response[ACCESS_TOKEN_KEY] == rebootstrapped[ACCESS_TOKEN_KEY]


async def test_run_with_reauth_reraises_when_there_is_neither_a_refresh_token_nor_an_npsso():
    message = lowercase_token()
    session = await _restored(RequestRecorder([]), _live_token_response(**{REFRESH_TOKEN_KEY: None}))

    async def operation():
        raise PsnAuthError(message)

    with pytest.raises(PsnAuthError, match=message):
        await session.run_with_reauth(operation)


async def test_a_transient_token_endpoint_failure_is_not_an_auth_failure():
    session = _session(RequestRecorder([httpx.Response(503)]), npsso=new_opaque_token())

    with pytest.raises(httpx.HTTPStatusError):
        await session._exchange(grant_type=REFRESH_TOKEN_GRANT, refresh_token=new_opaque_token())


async def test_a_token_response_without_expires_in_is_an_auth_failure():
    issued = _token_response()
    del issued[EXPIRES_IN_KEY]
    session = _session(RequestRecorder([httpx.Response(200, json=issued)]), npsso=new_opaque_token())

    with pytest.raises(PsnAuthError, match=EXPIRES_IN_KEY):
        await session._exchange(grant_type=REFRESH_TOKEN_GRANT, refresh_token=new_opaque_token())


async def test_run_with_reauth_reraises_the_original_error_when_the_refresh_grant_is_refused():
    message = lowercase_token()
    session = _session(RequestRecorder([httpx.Response(400)]))
    session.token_response = _live_token_response()

    async def operation():
        raise PsnAuthError(message)

    with pytest.raises(PsnAuthError, match=message):
        await session.run_with_reauth(operation)


async def test_a_read_retries_a_5xx_and_returns_the_eventual_success(no_retry_backoff):
    body = {lowercase_token(): lowercase_token()}
    recorder = RequestRecorder([httpx.Response(503), httpx.Response(200, json=body)])
    session = await _authenticated(recorder)

    response = await session.get(_psn_url())

    assert response.json() == body
    assert len(recorder.requests) == 2


async def test_a_read_gives_up_after_the_attempt_budget(no_retry_backoff):
    recorder = RequestRecorder([httpx.Response(503)] * READ_ATTEMPTS)
    session = await _authenticated(recorder)

    with pytest.raises(httpx.HTTPStatusError):
        await session.get(_psn_url())

    assert len(recorder.requests) == READ_ATTEMPTS


async def test_a_read_retries_a_transport_failure(no_retry_backoff):
    body = {lowercase_token(): lowercase_token()}
    attempts: list[httpx.Request] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError(lowercase_token(), request=request)
        return httpx.Response(200, json=body)

    session = await _restored(flaky, _live_token_response())

    response = await session.get(_psn_url())

    assert response.json() == body
    assert len(attempts) == 2


async def test_a_write_is_never_retried_because_it_could_apply_twice(no_retry_backoff):
    recorder = RequestRecorder([httpx.Response(503)])
    session = await _authenticated(recorder)

    with pytest.raises(httpx.HTTPStatusError):
        await session.post(_psn_url(), json={lowercase_token(): lowercase_token()})

    assert len(recorder.requests) == 1


async def test_a_read_does_not_retry_a_deliberate_4xx(no_retry_backoff):
    recorder = RequestRecorder([httpx.Response(404)])
    session = await _authenticated(recorder)

    with pytest.raises(httpx.HTTPStatusError):
        await session.get(_psn_url())

    assert len(recorder.requests) == 1


async def test_a_read_does_not_retry_an_auth_rejection(no_retry_backoff):
    recorder = RequestRecorder([httpx.Response(401)])
    session = await _authenticated(recorder)

    with pytest.raises(PsnAuthError):
        await session.get(_psn_url())

    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(500, True), (502, True), (503, True), (429, False), (404, False), (400, False)],
)
def test_only_server_side_failures_are_transient(status_code, retryable):
    request = httpx.Request(GET_METHOD, _psn_url())
    error = httpx.HTTPStatusError(
        lowercase_token(), request=request, response=httpx.Response(status_code, request=request)
    )

    assert is_transient_psn_failure(error) is retryable


def test_a_psn_auth_error_is_never_transient():
    assert is_transient_psn_failure(PsnAuthError(lowercase_token())) is False
