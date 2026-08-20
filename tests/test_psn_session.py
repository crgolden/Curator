"""Tests for PsnSession's async auth bootstrap, refresh, and request plumbing.

Ported from ``psnpy``'s ``test_psn_api.py``, rewritten async against ``httpx.MockTransport`` (the async
equivalent of that suite's ``requests.Session.request`` monkeypatch) instead of a real network call, with a
hand-written fake token store and rate limiter -- no ``unittest.mock`` anywhere, matching this repo's
testing convention.
"""

from __future__ import annotations

import time

import httpx
import pytest

from curator.psn.errors import PsnAuthError
from curator.psn.session import READ_ATTEMPTS, PsnSession, is_transient_psn_failure


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


def _fake_token_response(**overrides):
    body = {
        "access_token": "AT1",
        "refresh_token": "RT1",
        "token_type": "bearer",
        "expires_in": 3599,
        "scope": "psn:mobile.v2.core psn:clientapp",
        "refresh_token_expires_in": 5184000,
    }
    body.update(overrides)
    return body


class RequestRecorder:
    """Records every request an ``httpx.MockTransport`` receives and returns a queued response per call."""

    def __init__(self, responses: list[httpx.Response]):
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)


def _session(recorder: RequestRecorder, *, npsso=None, token_store=None, rate_limiter=None) -> PsnSession:
    client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    return PsnSession(npsso, token_store=token_store, rate_limiter=rate_limiter or FakeRateLimiter(), client=client)


async def test_bootstrap_from_npsso_sets_token():
    recorder = RequestRecorder(
        [
            httpx.Response(
                302,
                headers={"location": "com.scee.psxandroid.scecompcall://redirect?code=AUTHCODE123"},
            ),
            httpx.Response(200, json=_fake_token_response()),
        ]
    )
    session = _session(recorder, npsso="npsso-cookie")

    await session._ensure_fresh()

    assert session.token_response is not None
    assert session.token_response["access_token"] == "AT1"
    assert session.token_response["access_token_expires_at"] > time.time()
    assert recorder.requests[0].method == "GET"
    assert "authorize" in str(recorder.requests[0].url)
    assert recorder.requests[0].headers["Cookie"] == "npsso=npsso-cookie"
    assert recorder.requests[1].method == "POST"
    assert "token" in str(recorder.requests[1].url)


async def test_bootstrap_requests_offline_access():
    """access_type=offline is what makes PSN issue a refresh_token at all (see session.py's comment on
    _authorization_code()) -- a real production bug once had this missing, silently downgrading every user's
    link to an access-token-only session that expired in ~1 hour. This locks the parameter in.
    """
    recorder = RequestRecorder(
        [
            httpx.Response(
                302,
                headers={"location": "com.scee.psxandroid.scecompcall://redirect?code=AUTHCODE123"},
            ),
            httpx.Response(200, json=_fake_token_response()),
        ]
    )
    session = _session(recorder, npsso="npsso-cookie")

    await session._ensure_fresh()

    authorize_request = recorder.requests[0]
    assert authorize_request.url.params["access_type"] == "offline"


async def test_bootstrap_handles_response_without_refresh_token():
    recorder = RequestRecorder(
        [
            httpx.Response(
                302,
                headers={"location": "com.scee.psxandroid.scecompcall://redirect?code=AUTHCODE123"},
            ),
            httpx.Response(
                200,
                json={
                    "access_token": "AT1",
                    "token_type": "bearer",
                    "expires_in": 3599,
                    "scope": "psn:mobile.v2.core",
                },
            ),
        ]
    )
    session = _session(recorder, npsso="npsso-cookie")

    await session._ensure_fresh()

    assert session.token_response is not None
    assert session.token_response["access_token"] == "AT1"
    assert "refresh_token_expires_at" not in session.token_response


async def test_bootstrap_raises_on_expired_npsso():
    recorder = RequestRecorder(
        [
            httpx.Response(
                302,
                headers={"location": "com.scee.psxandroid.scecompcall://redirect?error=login_required&error_code=4165"},
            ),
        ]
    )
    session = _session(recorder, npsso="stale-cookie")

    with pytest.raises(PsnAuthError, match="expired or is incorrect"):
        await session._ensure_fresh()


async def test_restore_uses_cached_token_without_bootstrapping():
    recorder = RequestRecorder([])
    store = FakeTokenStore(saved=_fake_token_response(access_token_expires_at=time.time() + 3600))
    session = await PsnSession.restore(
        None,
        store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )

    await session._ensure_fresh()

    assert recorder.requests == []
    assert session.token_response is not None
    assert session.token_response["access_token"] == "AT1"


async def test_expired_cached_token_triggers_refresh_and_persists_it():
    recorder = RequestRecorder([httpx.Response(200, json=_fake_token_response(access_token="AT2"))])
    store = FakeTokenStore(saved=_fake_token_response(access_token_expires_at=time.time() - 10))
    session = await PsnSession.restore(
        None,
        store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )

    await session._ensure_fresh()

    assert session.token_response is not None
    assert session.token_response["access_token"] == "AT2"
    request_body = recorder.requests[0].content.decode()
    assert "grant_type=refresh_token" in request_body
    assert "refresh_token=RT1" in request_body
    assert store.saved_calls[-1]["access_token"] == "AT2"


async def test_restore_requires_npsso_or_cached_token():
    with pytest.raises(ValueError, match="No cached token and no npsso"):
        await PsnSession.restore(None, FakeTokenStore(saved=None))


async def test_get_attaches_bearer_and_ensures_fresh_token():
    recorder = RequestRecorder([httpx.Response(200, json={"ok": True})])
    store = FakeTokenStore(saved=_fake_token_response(access_token_expires_at=time.time() + 3600))
    session = await PsnSession.restore(
        None,
        store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )

    response = await session.get("https://m.np.playstation.com/api/thing", params={"a": "1"})

    assert response.json() == {"ok": True}
    request = recorder.requests[0]
    assert request.method == "GET"
    assert str(request.url) == "https://m.np.playstation.com/api/thing?a=1"
    assert request.headers["Authorization"] == "Bearer AT1"


async def test_post_raises_psn_auth_error_on_401():
    recorder = RequestRecorder([httpx.Response(401, text="unauthorized")])
    store = FakeTokenStore(saved=_fake_token_response(access_token_expires_at=time.time() + 3600))
    session = await PsnSession.restore(
        None,
        store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )

    with pytest.raises(PsnAuthError, match="401"):
        await session.post("https://m.np.playstation.com/api/thing", json={"x": 1})


async def test_patch_put_delete_attach_bearer():
    recorder = RequestRecorder(
        [
            httpx.Response(200, json={"ok": "patch"}),
            httpx.Response(200, json={"ok": "put"}),
            httpx.Response(200, json={}),
        ]
    )
    store = FakeTokenStore(saved=_fake_token_response(access_token_expires_at=time.time() + 3600))
    session = await PsnSession.restore(
        None,
        store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )

    await session.patch("https://m.np.playstation.com/api/thing", json={"a": 1})
    await session.put("https://m.np.playstation.com/api/thing")
    await session.delete("https://m.np.playstation.com/api/thing")

    assert [r.method for r in recorder.requests] == ["PATCH", "PUT", "DELETE"]
    assert all(r.headers["Authorization"] == "Bearer AT1" for r in recorder.requests)


async def test_get_raises_for_other_http_errors(no_retry_backoff):
    recorder = RequestRecorder([httpx.Response(500)] * READ_ATTEMPTS)
    store = FakeTokenStore(saved=_fake_token_response(access_token_expires_at=time.time() + 3600))
    session = await PsnSession.restore(
        None,
        store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )

    with pytest.raises(httpx.HTTPStatusError):
        await session.get("https://m.np.playstation.com/api/thing")


@pytest.mark.parametrize(
    "url",
    [
        "https://attacker.example/api/thing",
        "https://m.np.playstation.com.attacker.example/api/thing",
        "http://m.np.playstation.com/api/thing",
    ],
)
async def test_a_request_to_anything_but_a_psn_host_over_https_is_refused(url: str):
    recorder = RequestRecorder([httpx.Response(200, json={"ok": True})])
    store = FakeTokenStore(saved=_fake_token_response(access_token_expires_at=time.time() + 3600))
    session = await PsnSession.restore(None, store, client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)))

    with pytest.raises(ValueError, match="non-PSN URL"):
        await session.get(url)

    assert recorder.requests == []


async def test_a_path_that_escapes_its_endpoint_is_refused():
    recorder = RequestRecorder([httpx.Response(200, json={"ok": True})])
    store = FakeTokenStore(saved=_fake_token_response(access_token_expires_at=time.time() + 3600))
    session = await PsnSession.restore(None, store, client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)))

    with pytest.raises(ValueError, match="traversal segment"):
        await session.get("https://m.np.playstation.com/api/groups/../../admin")

    assert recorder.requests == []


async def test_every_psn_host_the_clients_use_is_allowed():
    for host in (
        "ca.account.sony.com",
        "m.np.playstation.com",
        "web.np.playstation.com",
        "accounts.api.playstation.com",
        "dms.api.playstation.com",
        "us-prof.np.community.playstation.net",
    ):
        assert PsnSession._verified_url(f"https://{host}/api/thing") == f"https://{host}/api/thing"


async def test_every_request_acquires_the_rate_limiter():
    recorder = RequestRecorder([httpx.Response(200, json={"ok": True})])
    store = FakeTokenStore(saved=_fake_token_response(access_token_expires_at=time.time() + 3600))
    rate_limiter = FakeRateLimiter()
    session = await PsnSession.restore(
        None,
        store,
        rate_limiter=rate_limiter,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )

    await session.get("https://m.np.playstation.com/api/thing")

    assert rate_limiter.acquire_calls == 1


async def test_run_with_reauth_refreshes_once_then_retries_when_there_is_no_npsso():
    rejected_access_token = httpx.Response(401, text="unauthorized")
    refresh_grant = httpx.Response(200, json=_fake_token_response(access_token="AT-REFRESHED"))
    retried_call = httpx.Response(200, json={"ok": True})
    recorder = RequestRecorder([rejected_access_token, refresh_grant, retried_call])
    session = _session(recorder)
    session.token_response = _fake_token_response(access_token_expires_at=time.time() + 3600)

    async def operation():
        return await session.get("https://m.np.playstation.com/api/thing")

    response = await session.run_with_reauth(operation)

    assert response.json() == {"ok": True}
    assert session.token_response is not None
    assert session.token_response["access_token"] == "AT-REFRESHED"
    assert recorder.requests[1].url.path.endswith("/token")


async def test_run_with_reauth_bootstraps_from_npsso_when_there_is_no_refresh_token():
    rejected_access_token = httpx.Response(401, text="unauthorized")
    authorization_code = httpx.Response(
        302, headers={"location": "com.scee.psxandroid.scecompcall://redirect?code=AUTHCODE123"}
    )
    bootstrap_grant = httpx.Response(200, json=_fake_token_response(access_token="AT-REBOOTSTRAPPED"))
    retried_call = httpx.Response(200, json={"ok": True})
    recorder = RequestRecorder([rejected_access_token, authorization_code, bootstrap_grant, retried_call])
    session = _session(recorder, npsso="npsso-cookie")
    session.token_response = _fake_token_response(access_token_expires_at=time.time() + 3600, refresh_token="")

    async def operation():
        return await session.get("https://m.np.playstation.com/api/thing")

    response = await session.run_with_reauth(operation)

    assert response.json() == {"ok": True}
    assert session.token_response is not None
    assert session.token_response["access_token"] == "AT-REBOOTSTRAPPED"


async def test_run_with_reauth_reraises_when_there_is_neither_a_refresh_token_nor_an_npsso():
    recorder = RequestRecorder([])
    store = FakeTokenStore(saved=_fake_token_response(access_token_expires_at=time.time() + 3600, refresh_token=""))
    session = await PsnSession.restore(
        None,
        store,
        client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)),
    )

    async def operation():
        raise PsnAuthError("boom")

    with pytest.raises(PsnAuthError, match="boom"):
        await session.run_with_reauth(operation)


async def test_a_transient_token_endpoint_failure_is_not_an_auth_failure():
    session = _session(RequestRecorder([httpx.Response(503, text="service unavailable")]), npsso="npsso-cookie")

    with pytest.raises(httpx.HTTPStatusError):
        await session._exchange(grant_type="refresh_token", refresh_token="RT1")


async def test_a_token_response_without_expires_in_is_an_auth_failure():
    session = _session(RequestRecorder([httpx.Response(200, json={"access_token": "AT1"})]), npsso="npsso-cookie")

    with pytest.raises(PsnAuthError, match="expires_in"):
        await session._exchange(grant_type="refresh_token", refresh_token="RT1")


async def test_run_with_reauth_reraises_the_original_error_when_the_refresh_grant_is_refused():
    recorder = RequestRecorder([httpx.Response(400, text="invalid_grant")])
    session = _session(recorder)
    session.token_response = _fake_token_response(access_token_expires_at=time.time() + 3600)

    async def operation():
        raise PsnAuthError("boom")

    with pytest.raises(PsnAuthError, match="boom"):
        await session.run_with_reauth(operation)


async def _authenticated(recorder: RequestRecorder) -> PsnSession:
    store = FakeTokenStore(saved=_fake_token_response(access_token_expires_at=time.time() + 3600))
    return await PsnSession.restore(None, store, client=httpx.AsyncClient(transport=httpx.MockTransport(recorder)))


async def test_a_read_retries_a_5xx_and_returns_the_eventual_success(no_retry_backoff):
    recorder = RequestRecorder([httpx.Response(503), httpx.Response(200, json={"ok": True})])
    session = await _authenticated(recorder)

    response = await session.get("https://m.np.playstation.com/api/thing")

    assert response.json() == {"ok": True}
    assert len(recorder.requests) == 2


async def test_a_read_gives_up_after_the_attempt_budget(no_retry_backoff):
    recorder = RequestRecorder([httpx.Response(503)] * READ_ATTEMPTS)
    session = await _authenticated(recorder)

    with pytest.raises(httpx.HTTPStatusError):
        await session.get("https://m.np.playstation.com/api/thing")

    assert len(recorder.requests) == READ_ATTEMPTS


async def test_a_read_retries_a_transport_failure(no_retry_backoff):
    attempts: list[httpx.Request] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, json={"ok": True})

    store = FakeTokenStore(saved=_fake_token_response(access_token_expires_at=time.time() + 3600))
    session = await PsnSession.restore(None, store, client=httpx.AsyncClient(transport=httpx.MockTransport(flaky)))

    response = await session.get("https://m.np.playstation.com/api/thing")

    assert response.json() == {"ok": True}
    assert len(attempts) == 2


async def test_a_write_is_never_retried_because_it_could_apply_twice(no_retry_backoff):
    recorder = RequestRecorder([httpx.Response(503)])
    session = await _authenticated(recorder)

    with pytest.raises(httpx.HTTPStatusError):
        await session.post("https://m.np.playstation.com/api/thing", json={"x": 1})

    assert len(recorder.requests) == 1


async def test_a_read_does_not_retry_a_deliberate_4xx(no_retry_backoff):
    recorder = RequestRecorder([httpx.Response(404)])
    session = await _authenticated(recorder)

    with pytest.raises(httpx.HTTPStatusError):
        await session.get("https://m.np.playstation.com/api/thing")

    assert len(recorder.requests) == 1


async def test_a_read_does_not_retry_an_auth_rejection(no_retry_backoff):
    recorder = RequestRecorder([httpx.Response(401, text="unauthorized")])
    session = await _authenticated(recorder)

    with pytest.raises(PsnAuthError):
        await session.get("https://m.np.playstation.com/api/thing")

    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    ("status_code", "retryable"),
    [(500, True), (502, True), (503, True), (429, False), (404, False), (400, False)],
)
def test_only_server_side_failures_are_transient(status_code, retryable):
    request = httpx.Request("GET", "https://m.np.playstation.com/api/thing")
    error = httpx.HTTPStatusError("boom", request=request, response=httpx.Response(status_code, request=request))

    assert is_transient_psn_failure(error) is retryable


def test_a_psn_auth_error_is_never_transient():
    assert is_transient_psn_failure(PsnAuthError("401")) is False
