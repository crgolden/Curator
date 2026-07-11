"""JWT Bearer access-token validation against Duende IdentityServer's published JWKS.

Curator is a pure resource server: it never issues tokens, never redirects a browser through a login
flow, and holds no session of its own (see ``README.md``'s auth section). Every protected route instead
presents an access token Identity minted, and :class:`JwtValidator` is where that token earns trust.
Validation mirrors the sibling ``Directory`` .NET API's ``JwtBearerOptions`` (``Directory/Program.cs``):
RS256 signature verified against Identity's discovery-published JWKS, ``iss`` checked against the
configured authority, ``exp``/``nbf`` checked -- but **not** ``aud`` (``ValidateAudience = false`` there;
Identity issues tokens with no Curator-specific audience, so Curator doesn't check for one either).

JWKS/discovery fetching is injected (``fetch_json``) so unit tests can serve canned documents with no
network access at all; the default implementation is a small ``urllib``-based HTTP GET. The fetched JWKS
is cached on the instance and only refetched when a token's ``kid`` isn't found in it -- covering
Identity's normal key-rotation story (a new signing key appears in the JWKS; a token signed with it
shouldn't be rejected just because Curator's cache predates the rotation).
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Optional, Protocol

from authlib.jose import JsonWebKey, JsonWebToken
from authlib.jose.errors import JoseError

_ALGORITHMS = ["RS256"]


class TokenError(Exception):
    """Raised when a bearer token fails validation for any reason: bad signature, wrong issuer, expired/
    not-yet-valid, malformed, or missing its ``sub`` claim. The message is always safe to surface as an
    HTTP 401 detail -- it never includes the raw token.
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
    """

    sub: str
    email: Optional[str]
    iat: datetime
    scopes: tuple[str, ...]

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


def fetch_json(url: str) -> dict:
    """Default ``fetch_json``: a plain HTTP GET, JSON-decoded. Injected so tests never hit the network.

    :param url: The URL to fetch (Identity's discovery document, or the ``jwks_uri`` it points to).
    :returns: The parsed JSON body.
    """
    with urllib.request.urlopen(url) as response:  # noqa: S310 - fixed https URL derived from config only
        return json.loads(response.read().decode("utf-8"))


class JwtValidator:
    """Validates RS256 access tokens against a Duende IdentityServer authority's published JWKS.

    :param authority: The Identity OIDC authority base URL. Its discovery document is fetched from
        ``{authority}/.well-known/openid-configuration``, whose ``jwks_uri`` is then fetched for the
        signing keys.
    :param fetch_json: A ``url -> dict`` callable used for both fetches; defaults to a small
        ``urllib``-based GET. Tests inject a fake that serves canned discovery/JWKS documents.
    """

    def __init__(self, authority: str, fetch_json: Callable[[str], dict] = fetch_json) -> None:
        self._authority = authority.rstrip("/")
        self._fetch_json = fetch_json
        self._jwt = JsonWebToken(_ALGORITHMS)
        self._keyset = None

    def validate(self, token: str) -> TokenClaims:
        """Validate ``token`` and extract the claims Curator cares about.

        :param token: The raw JWT (the part after ``Bearer `` in the ``Authorization`` header).
        :returns: The extracted :class:`TokenClaims`.
        :raises TokenError: If the token is malformed; its signature doesn't verify against any known key
            (even after one refetch of the JWKS for an unrecognized ``kid``); its ``iss`` doesn't match
            ``authority``; it is expired or not yet valid; or it carries no ``sub``/``iat`` claim.
        """
        claims = self._decode(token)

        try:
            claims.validate()
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
        )

    def _decode(self, token: str):
        """Decode and structurally validate ``token``'s signature, refetching the JWKS once on an
        unrecognized ``kid`` before giving up.
        """
        options = {"iss": {"essential": True, "value": self._authority}}
        keyset = self._ensure_keyset()
        try:
            return self._jwt.decode(token, keyset, claims_options=options)
        except ValueError:
            pass  # unknown kid: fall through to a forced refetch-and-retry, below
        except JoseError as exc:
            raise TokenError(f"Malformed or unverifiable token: {exc}") from exc

        keyset = self._ensure_keyset(force=True)
        try:
            return self._jwt.decode(token, keyset, claims_options=options)
        except (ValueError, JoseError) as exc:
            raise TokenError(f"Malformed or unverifiable token: {exc}") from exc

    def _ensure_keyset(self, *, force: bool = False):
        """Return the cached :class:`~authlib.jose.KeySet`, fetching (or refetching) it when needed."""
        if self._keyset is None or force:
            discovery = self._fetch_json(f"{self._authority}/.well-known/openid-configuration")
            jwks = self._fetch_json(discovery["jwks_uri"])
            self._keyset = JsonWebKey.import_key_set(jwks)
        return self._keyset


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
