"""JWT Bearer access-token validation against Duende IdentityServer's published JWKS.

Curator is a pure resource server: it never issues tokens, never redirects a browser through a login
flow, and holds no session of its own (see ``README.md``'s auth section). Every protected route instead
presents an access token Identity minted, and :class:`JwtValidator` is where that token earns trust.
Validation verifies the RS256 signature against Identity's discovery-published JWKS, checks ``iss``
against the configured authority, and checks ``exp``/``nbf`` -- but **not** ``aud``: Identity issues
tokens with no Curator-specific audience, so Curator doesn't check for one.

JWKS/discovery fetching is injected (``fetch_json``) so unit tests can serve canned documents with no
network access at all; the default implementation is a small ``urllib``-based HTTP GET. The fetched JWKS
is cached on the instance and only refetched when a token's ``kid`` isn't found in it -- covering
Identity's normal key-rotation story (a new signing key appears in the JWKS; a token signed with it
shouldn't be rejected just because Curator's cache predates the rotation).

Two failures live here and they are not the same failure. A token that fails validation raises
:class:`TokenError` and is a 401. Identity being unreachable raises :class:`AuthorityUnavailableError` and
is a 503 -- Curator reached no verdict on the token at all, and saying "your credential is bad" when the
credential was never examined sends the caller back to the service that is down.
"""

from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol, cast

from joserfc import jwt
from joserfc.errors import InvalidKeyIdError, JoseError
from joserfc.jwk import KeySet, KeySetSerialization
from joserfc.jwt import JWTClaimsRegistry

_ALGORITHMS = ["RS256"]

_DISCOVERY_FETCH_TIMEOUT_SECONDS = 10.0

AUTHORITY_UNAVAILABLE_DETAIL = "Identity is unavailable, so the bearer token could not be verified."
"""The 503 detail :class:`AuthorityUnavailableError` carries. Names the dependency without echoing the
configured authority URL back to a caller; the ``raise ... from`` chain keeps the real cause for the span.
"""


class TokenError(Exception):
    """Raised when a bearer token fails validation for any reason: bad signature, wrong issuer, expired/
    not-yet-valid, malformed, or missing its ``sub`` claim. The message is always safe to surface as an
    HTTP 401 detail -- it never includes the raw token.
    """


class AuthorityUnavailableError(Exception):
    """Raised when Identity could not be reached for its signing keys, so no verdict on the token was ever
    reached.

    Deliberately **not** a :class:`TokenError`, and deliberately not a subclass of anything
    :meth:`JwtValidator._decode` catches. A ``TokenError`` becomes a 401 carrying
    ``WWW-Authenticate: Bearer``, which tells the caller their credential is bad -- and Librarian's BFF
    treats exactly that pair as "refresh the token and retry once"
    (``Librarian/src/bff/proxy.ts``'s 401 branch), sending a refresh to the very service that is down.
    The honest answer is a 503 naming the dependency; see :func:`curator.deps.require_bearer`.

    Before this existed, a discovery/JWKS failure escaped :meth:`JwtValidator.validate` uncaught and
    reached ``create_app``'s catch-all handler as a bare ``text/plain`` 500 on every authenticated route
    at once -- an outage of the identity provider presenting as Curator being broken, with nothing in the
    response naming the real dependency.
    """


@dataclass(frozen=True)
class TokenClaims:
    """The claims Curator cares about, extracted from a validated access token.

    :param sub: The Identity ``sub`` claim -- Curator's sole user identifier.
    :param email: The ``email`` user claim carried by the ``curator`` ApiScope, if present. ``None`` when
        the token has no email claim at all; routes that need it (see ``curator.deps.require_verified_caller``)
        reject that case with a 403 rather than treating it as an anonymous/absent value.
    :param iat: When the token was issued (aware UTC). Used to decide whether a stored PSN link needs
        re-verification against this token (see ``curator.reverify.reverify_link``): a token issued after
        the link's last verification triggers a fresh check, an older/same-vintage token does not.
    :param scopes: Every scope the token carries, parsed from either a JSON array (Duende's JWT scope
        shape) or a legacy space-delimited string.
    :param is_admin: The ``curator.admin`` claim, mirroring the ``Directory`` API's ``churches.mod``
        elevated-claim pattern -- ``True`` only when Identity's token explicitly carries
        ``"curator.admin": true``. Gates ``POST /enrichment/runs`` (see :func:`curator.deps.require_admin`);
        every other route only requires the plain ``curator`` scope.
    """

    sub: str
    email: str | None
    iat: datetime
    scopes: tuple[str, ...]
    is_admin: bool = False

    def has_scope(self, scope: str) -> bool:
        """Return whether ``scope`` is present among this token's scopes.

        :param scope: The scope to look for (e.g. ``"curator"``).
        """
        return scope in self.scopes


class TokenValidatorLike(Protocol):
    """The shape an injected token validator must satisfy: a single ``validate(token) -> TokenClaims``."""

    def validate(self, token: str) -> TokenClaims:
        """Validate a raw JWT and return its extracted claims, or raise :class:`TokenError`."""
        ...


def fetch_json(url: str) -> dict[str, Any]:
    """Default ``fetch_json``: a plain HTTP GET, JSON-decoded. Injected so tests never hit the network.

    :param url: The URL to fetch (Identity's discovery document, or the ``jwks_uri`` it points to).
    :returns: The parsed JSON body.
    :raises OSError: On a network failure, including a timeout.
    """
    with urllib.request.urlopen(url, timeout=_DISCOVERY_FETCH_TIMEOUT_SECONDS) as response:
        body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        return body


class JwtValidator:
    """Validates RS256 access tokens against a Duende IdentityServer authority's published JWKS.

    :param authority: The Identity OIDC authority base URL. Its discovery document is fetched from
        ``{authority}/.well-known/openid-configuration``, whose ``jwks_uri`` is then fetched for the
        signing keys.
    :param fetch_json: A ``url -> dict`` callable used for both fetches; defaults to a small
        ``urllib``-based GET. Tests inject a fake that serves canned discovery/JWKS documents.
    """

    def __init__(self, authority: str, fetch_json: Callable[[str], dict[str, Any]] = fetch_json) -> None:
        self._authority = authority.rstrip("/")
        self._fetch_json = fetch_json
        self._claims_registry = JWTClaimsRegistry(iss={"essential": True, "value": self._authority})
        self._keyset: KeySet | None = None

    def validate(self, token: str) -> TokenClaims:
        """Validate ``token`` and extract the claims Curator cares about.

        :param token: The raw JWT (the part after ``Bearer `` in the ``Authorization`` header).
        :returns: The extracted :class:`TokenClaims`.
        :raises TokenError: If the token is malformed; its signature doesn't verify against any known key
            (even after one refetch of the JWKS for an unrecognized ``kid``); its ``iss`` doesn't match
            ``authority``; it is expired or not yet valid; or it carries no ``sub``/``iat`` claim.
        :raises AuthorityUnavailableError: If Identity could not supply the signing keys at all, so the
            token was never judged either way. A different outcome from every :class:`TokenError` above,
            and it must stay a different HTTP status.
        """
        claims = self._decode(token)

        try:
            self._claims_registry.validate(claims)
        except JoseError as exc:
            raise TokenError(str(exc)) from exc

        sub = claims.get("sub")
        if not sub:
            raise TokenError("Token carries no sub claim.")

        iat = claims.get("iat")
        if iat is None:
            raise TokenError("Token carries no iat claim.")

        return TokenClaims(
            sub=sub,
            email=claims.get("email"),
            iat=datetime.fromtimestamp(iat, tz=timezone.utc),
            scopes=_parse_scopes(claims.get("scope")),
            is_admin=_is_true(claims.get("curator.admin")),
        )

    def _decode(self, token: str) -> dict[str, Any]:
        """Decode and structurally validate ``token``'s signature, refetching the JWKS once on an
        unrecognized ``kid`` before giving up.
        """
        keyset = self._ensure_keyset()
        try:
            return jwt.decode(token, keyset, algorithms=_ALGORITHMS).claims
        except InvalidKeyIdError:
            pass
        except JoseError as exc:
            raise TokenError(f"Malformed or unverifiable token: {exc}") from exc

        keyset = self._ensure_keyset(force=True)
        try:
            return jwt.decode(token, keyset, algorithms=_ALGORITHMS).claims
        except JoseError as exc:
            raise TokenError(f"Malformed or unverifiable token: {exc}") from exc

    def _ensure_keyset(self, *, force: bool = False) -> KeySet:
        """Return the cached :class:`~joserfc.jwk.KeySet`, fetching (or refetching) it when needed.

        :param force: Refetch even when a keyset is already cached -- how :meth:`_decode` handles a
            ``kid`` the cache predates.
        :returns: The signing keys Identity publishes.
        :raises AuthorityUnavailableError: If Identity could not be reached (``OSError``, covering
            ``urllib.error.HTTPError``/``URLError`` and the timeout), answered with something that is not
            JSON (``ValueError``), answered with JSON that is not a discovery document (``KeyError`` on
            ``jwks_uri``), or published a JWKS ``joserfc`` cannot import (:class:`~joserfc.errors.JoseError`).
            All four say the same thing about the caller's token, which is nothing.
        """
        if self._keyset is None or force:
            try:
                discovery = self._fetch_json(f"{self._authority}/.well-known/openid-configuration")
                jwks = cast(KeySetSerialization, self._fetch_json(discovery["jwks_uri"]))
                keyset = KeySet.import_key_set(jwks)
            except (OSError, ValueError, KeyError, JoseError) as exc:
                raise AuthorityUnavailableError(AUTHORITY_UNAVAILABLE_DETAIL) from exc
            self._keyset = keyset
        return self._keyset


def _is_true(raw: object) -> bool:
    """Normalize a claim value that should represent a boolean.

    Duende emits custom boolean claims as either a JSON boolean or the string ``"true"``/``"True"``
    depending on the claim-mapping configuration; this accepts both rather than assuming one.

    :param raw: The raw claim value: a bool, a string, or ``None``.
    :returns: ``True`` only if ``raw`` is ``True`` or the case-insensitive string ``"true"``.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.lower() == "true"
    return False


def _parse_scopes(raw: object) -> tuple[str, ...]:
    """Normalize a token's ``scope`` claim into a tuple, accepting Duende's JSON-array form as well as a
    legacy space-delimited string.

    :param raw: The raw ``scope`` claim value: a list/tuple, a string, or ``None``.
    :returns: The parsed scopes, or an empty tuple if ``raw`` is falsy/unrecognized.
    """
    if isinstance(raw, (list, tuple)):
        return tuple(raw)
    if isinstance(raw, str):
        return tuple(raw.split())
    return ()
