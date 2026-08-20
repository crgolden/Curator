"""Async PSN HTTP session: auth bootstrap, refresh, and rate-throttled GET/POST/PATCH/PUT/DELETE.

Built on ``httpx.AsyncClient`` with an ``asyncio``-based, Redis-backed distributed rate limiter.
Implements PSN's private OAuth2 flow directly (npsso cookie -> authorization code -> access/refresh token
exchange) and a bearer-token request pattern -- no TLS-impersonation trick is needed; a live spike
against a real account found no fingerprint blocking from PSN/Akamai.

Every other ``curator.psn.*`` client is built on this one shared engine.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Any, Protocol, TypeVar
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from curator.http_client import shared_ssl_context
from curator.psn.errors import PsnAuthError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Mapping

T = TypeVar("T")

_AUTH_BASE = "https://ca.account.sony.com/api/authz/v3/oauth"
_TOKEN_REJECTION_STATUS_CODES = frozenset({400, 401, 403})
_CLIENT_ID = "09515159-7237-4370-9b40-3806e67c0891"
_SCOPE = "psn:mobile.v2.core psn:clientapp"
_REDIRECT_URI = "com.scee.psxandroid.scecompcall://redirect"
_BASIC_AUTH = "Basic MDk1MTUxNTktNzIzNy00MzcwLTliNDAtMzgwNmU2N2MwODkxOnVjUGprYTV0bnRCMktxc1A="
_TOKEN_USER_AGENT = "com.sony.snei.np.android.sso.share.oauth.versa.USER_AGENT"

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Mobile Safari/537.36"
)

RATE_LIMIT_MAX_REQUESTS = 300
RATE_LIMIT_WINDOW_SECONDS = 15 * 60

RETRYABLE_METHODS = frozenset({"GET"})
RETRYABLE_STATUS_FLOOR = 500
READ_ATTEMPTS = 3
UNRETRIED_ATTEMPTS = 1
RETRY_BACKOFF_MULTIPLIER_SECONDS = 0.5
RETRY_MAX_WAIT_SECONDS = 4.0

_ALLOWED_HOSTS: frozenset[str] = frozenset(
    {
        "ca.account.sony.com",
        "m.np.playstation.com",
        "web.np.playstation.com",
        "accounts.api.playstation.com",
        "dms.api.playstation.com",
        "us-prof.np.community.playstation.net",
    }
)


def is_transient_psn_failure(exception: BaseException) -> bool:
    """Whether a failed PSN call is worth another attempt.

    :param exception: The exception a request attempt raised.
    :returns: ``True`` for a network-level failure or a ``5xx``; ``False`` for anything the server
        answered deliberately -- :class:`~curator.psn.errors.PsnAuthError` (``401``/``403``), any other
        ``4xx``, and a rejected URL all mean the same call would fail identically on a retry.
    """
    if isinstance(exception, httpx.TransportError):
        return True
    if isinstance(exception, httpx.HTTPStatusError):
        return exception.response.status_code >= RETRYABLE_STATUS_FLOOR
    return False


class TokenStore(Protocol):
    """Duck-typed async token cache contract, satisfied by :class:`curator.persistence.db_token_store.DbTokenStore`."""

    async def load(self) -> dict[str, Any] | None:
        """Load the cached token response, or ``None`` if absent."""
        ...

    async def save(self, token_response: dict[str, Any]) -> None:
        """Persist a token response, replacing any previous value."""
        ...

    async def clear(self) -> None:
        """Remove the cached token, if present."""
        ...


class RateLimiter(Protocol):
    """A distributed PSN request-rate throttle: blocks (asynchronously) until a request is within budget."""

    async def acquire(self) -> None:
        """Block until the caller is within PSN's request budget, then record this request."""
        ...


class NullRateLimiter:
    """A no-op :class:`RateLimiter` -- used in tests that don't care about throttling behavior."""

    async def acquire(self) -> None:
        """Never throttle."""


class PsnSession:
    """A native, ``httpx``-based authenticated async PSN session.

    Injectable into every ``curator.psn.*_client`` module as a dependency-injection/testing seam.

    :param npsso: The npsso cookie; required only when there is no usable cached token.
    :param token_store: Optional token cache; when present, the token is restored via :meth:`create`.
    :param rate_limiter: The distributed rate throttle; defaults to :class:`NullRateLimiter` (no throttling).
    :param client: The underlying :class:`httpx.AsyncClient`; defaults to a new one with PSN's expected headers.
    """

    def __init__(
        self,
        npsso: str | None = None,
        *,
        token_store: TokenStore | None = None,
        rate_limiter: RateLimiter | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._npsso = npsso
        self._token_store = token_store
        self._cid = str(uuid.UUID(int=uuid.getnode()))
        self._rate_limiter = rate_limiter or NullRateLimiter()
        self._client = client or httpx.AsyncClient(
            headers={
                "User-Agent": _DEFAULT_USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
                "Country": "US",
            },
            timeout=15.0,
            verify=shared_ssl_context(),
        )
        self.token_response: dict[str, Any] | None = None

    @classmethod
    async def restore(
        cls,
        npsso: str | None,
        token_store: TokenStore,
        *,
        rate_limiter: RateLimiter | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> PsnSession:
        """Build a session, restoring a cached token when available.

        :param npsso: The npsso token, required only when there is no usable cached token.
        :param token_store: The store to read the cached token from.
        :param rate_limiter: The distributed rate throttle; defaults to no throttling.
        :param client: The underlying :class:`httpx.AsyncClient`; defaults to a new one.
        :returns: A ready :class:`PsnSession`.
        :raises ValueError: If there is neither a cached token nor an npsso.
        """
        session = cls(npsso, token_store=token_store, rate_limiter=rate_limiter, client=client)
        saved = await token_store.load()
        if saved is not None:
            session.token_response = saved
        elif not npsso:
            raise ValueError(
                "No cached token and no npsso provided. Supply an npsso to authenticate for the first time."
            )
        return session

    async def aclose(self) -> None:
        """Close the underlying :class:`httpx.AsyncClient`."""
        await self._client.aclose()

    async def run_with_reauth(self, operation: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Run an async operation, re-bootstrapping from npsso once if PSN rejects the current token.

        Retry-once semantics. Token *persistence* is not a separate step a client must remember to call:
        :meth:`_exchange` saves to the token store as soon as a fresh token is obtained, so this only
        needs to handle the retry.

        :param operation: A zero-argument async callable to run (typically a client's private ``_op``
            method bound with its arguments via a closure).
        :returns: The operation's result.
        :raises PsnAuthError: If the operation still fails after one recovery attempt, or there is neither
            a refresh token nor an npsso to recover with.
        """
        try:
            return await operation()
        except PsnAuthError:
            if not await self._reauthenticate():
                raise
            return await operation()

    async def _reauthenticate(self) -> bool:
        """Obtain a way to retry after PSN rejected the current token, or report that there is none.

        :returns: Whether the caller may retry the operation once.
        """
        if self.token_response is not None and self.token_response.get("refresh_token"):
            try:
                await self._refresh()
                return True
            except PsnAuthError:
                pass
        if not self._npsso:
            return False
        self.token_response = None
        return True

    async def _ensure_fresh(self) -> None:
        """Bootstrap from npsso, or refresh the access token, if needed before a request."""
        if self.token_response is None:
            await self._bootstrap()
            return
        if self.token_response.get("access_token_expires_at", 0) > time.time():
            return
        await self._refresh()

    async def _bootstrap(self) -> None:
        """Perform the full npsso -> authorization code -> access token flow.

        :raises PsnAuthError: If there is no npsso to bootstrap from, or PSN rejects the authorization.
        """
        if not self._npsso:
            raise PsnAuthError("No npsso cookie available to bootstrap a new session.")
        code = await self._authorization_code()
        await self._exchange(grant_type="authorization_code", code=code)

    async def _authorization_code(self) -> str:
        """Exchange the npsso cookie for a one-time authorization code (the redirect is never followed)."""
        headers = {
            "Cookie": f"npsso={self._npsso}",
            "X-Requested-With": "com.scee.psxandroid",
        }
        params = {
            "access_type": "offline",
            "client_id": _CLIENT_ID,
            "redirect_uri": _REDIRECT_URI,
            "scope": _SCOPE,
            "response_type": "code",
            "cid": self._cid,
        }
        response = await self._throttled_request(
            "GET",
            f"{_AUTH_BASE}/authorize",
            headers=headers,
            params=params,
            follow_redirects=False,
        )
        location = response.headers.get("location", "")
        query = parse_qs(urlparse(location).query)
        if "error" in query:
            raise PsnAuthError("Your npsso code has expired or is incorrect. Please generate a new one.")
        if "code" not in query:
            raise PsnAuthError(f"PSN authorization did not return a code (status {response.status_code}).")
        return str(query["code"][0])

    async def _refresh(self) -> None:
        """Refresh the access token using the stored refresh token.

        :raises PsnAuthError: If there is no refresh token to use.
        """
        if self.token_response is None or not self.token_response.get("refresh_token"):
            raise PsnAuthError("No refresh token available.")
        await self._exchange(grant_type="refresh_token", refresh_token=self.token_response["refresh_token"])

    async def _exchange(self, *, grant_type: str, code: str | None = None, refresh_token: str | None = None) -> None:
        """POST to PSN's token endpoint for either the authorization-code or refresh-token grant."""
        headers = {
            "Authorization": _BASIC_AUTH,
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": _TOKEN_USER_AGENT,
        }
        data = {"grant_type": grant_type, "scope": _SCOPE, "token_format": "jwt"}
        if code is not None:
            data.update(code=code, redirect_uri=_REDIRECT_URI, cid=self._cid)
        if refresh_token is not None:
            data["refresh_token"] = refresh_token

        response = await self._throttled_request("POST", f"{_AUTH_BASE}/token", headers=headers, data=data)
        if response.status_code in _TOKEN_REJECTION_STATUS_CODES:
            raise PsnAuthError(f"PSN token exchange failed ({response.status_code}): {response.text[:200]}")
        response.raise_for_status()

        token: dict[str, Any] = response.json()
        expires_in = token.get("expires_in")
        if not isinstance(expires_in, (int, float)):
            raise PsnAuthError("PSN's token response carried no usable expires_in.")
        now = time.time()
        token["access_token_expires_at"] = expires_in + now
        if "refresh_token_expires_in" in token:
            token["refresh_token_expires_at"] = token["refresh_token_expires_in"] + now
        self.token_response = token
        if self._token_store is not None:
            await self._token_store.save(token)

    async def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Make an authenticated GET request, bootstrapping/refreshing the token first if needed.

        :param url: The request URL.
        :param params: Query parameters.
        :param headers: Extra request headers (merged over the defaults; ``Authorization`` is always overridden).
        :returns: The ``httpx`` response.
        :raises PsnAuthError: If the response is ``401``/``403`` (an expired/invalid token or authorization).
        """
        return await self._request("GET", url, params=params, headers=headers)

    async def post(
        self,
        url: str,
        *,
        json: Any = None,
        data: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Make an authenticated POST request, bootstrapping/refreshing the token first if needed.

        :param url: The request URL.
        :param json: A JSON request body.
        :param data: A form-encoded request body.
        :param params: Query parameters.
        :param headers: Extra request headers (merged over the defaults; ``Authorization`` is always overridden).
        :returns: The ``httpx`` response.
        :raises PsnAuthError: If the response is ``401``/``403`` (an expired/invalid token or authorization).
        """
        return await self._request("POST", url, params=params, headers=headers, json=json, data=data)

    async def patch(
        self,
        url: str,
        *,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> httpx.Response:
        """Make an authenticated PATCH request, bootstrapping/refreshing the token first if needed.

        :param url: The request URL.
        :param json: A JSON request body.
        :param headers: Extra request headers (merged over the defaults; ``Authorization`` is always overridden).
        :returns: The ``httpx`` response.
        :raises PsnAuthError: If the response is ``401``/``403`` (an expired/invalid token or authorization).
        """
        return await self._request("PATCH", url, headers=headers, json=json)

    async def put(self, url: str, *, headers: Mapping[str, str] | None = None) -> httpx.Response:
        """Make an authenticated PUT request, bootstrapping/refreshing the token first if needed.

        :param url: The request URL.
        :param headers: Extra request headers (merged over the defaults; ``Authorization`` is always overridden).
        :returns: The ``httpx`` response.
        :raises PsnAuthError: If the response is ``401``/``403`` (an expired/invalid token or authorization).
        """
        return await self._request("PUT", url, headers=headers)

    async def delete(self, url: str, *, headers: Mapping[str, str] | None = None) -> httpx.Response:
        """Make an authenticated DELETE request, bootstrapping/refreshing the token first if needed.

        :param url: The request URL.
        :param headers: Extra request headers (merged over the defaults; ``Authorization`` is always overridden).
        :returns: The ``httpx`` response.
        :raises PsnAuthError: If the response is ``401``/``403`` (an expired/invalid token or authorization).
        """
        return await self._request("DELETE", url, headers=headers)

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Make one authenticated PSN call, retrying a read that failed transiently.

        Only the methods in :data:`RETRYABLE_METHODS` are retried. A ``5xx`` on a write funnels through
        here too, and PSN gives no way to tell "the write failed" from "the write succeeded and the
        response was lost" -- so a retried ``POST``/``PATCH``/``PUT``/``DELETE`` risks applying the
        change twice. Reads carry no such risk, which is the whole reason the split exists.

        Each attempt re-enters :meth:`_throttled_request` and therefore re-acquires rate-limit budget,
        so retries are paced by the same fleet-wide throttle as first attempts.
        """
        attempts = READ_ATTEMPTS if method in RETRYABLE_METHODS else UNRETRIED_ATTEMPTS
        retrying = AsyncRetrying(
            stop=stop_after_attempt(attempts),
            wait=wait_exponential(multiplier=RETRY_BACKOFF_MULTIPLIER_SECONDS, max=RETRY_MAX_WAIT_SECONDS),
            retry=retry_if_exception(is_transient_psn_failure),
            reraise=True,
        )
        return await retrying(self._send_once, method, url, **kwargs)

    async def _send_once(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        await self._ensure_fresh()
        headers = dict(kwargs.pop("headers", None) or {})
        token_response = self.token_response
        assert token_response is not None
        headers["Authorization"] = f"Bearer {token_response['access_token']}"
        response = await self._throttled_request(method, url, headers=headers, **kwargs)
        if response.status_code in (401, 403):
            raise PsnAuthError(f"PSN request unauthorized/forbidden ({response.status_code}).")
        response.raise_for_status()
        return response

    @staticmethod
    def _verified_url(url: str) -> str:
        """Return ``url`` if it addresses a known PSN host over HTTPS with no traversal segment.

        :param url: The absolute request URL.
        :returns: ``url``, unchanged.
        :raises ValueError: If the scheme is not HTTPS, the host is not a PSN host, or the path contains
            a ``..`` segment once percent-decoded.
        """
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS:
            raise ValueError(f"Refusing a PSN request to a non-PSN URL: {parsed.scheme}://{parsed.hostname}")
        if ".." in unquote(parsed.path).replace("\\", "/").split("/"):
            raise ValueError("Refusing a PSN request whose path contains a traversal segment.")
        return url

    async def _throttled_request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Acquire the distributed rate-limit budget, then make the request.

        :attr:`_rate_limiter` is expected to be backed by a shared store (Redis) when Curator scales out
        across multiple App Service instances, so the budget is enforced fleet-wide, not per-process.
        """
        verified = self._verified_url(url)
        await self._rate_limiter.acquire()
        return await self._client.request(method, verified, **kwargs)
