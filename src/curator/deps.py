"""FastAPI dependencies shared across Curator's routers.

Curator is a pure JWT Bearer resource server (see ``README.md``'s auth section) -- there is no session,
no cookie, no login redirect. :func:`require_bearer` is the single gate every protected route depends on:
read the ``Authorization: Bearer <token>`` header, validate it via the app's configured
:class:`~curator.token_validation.JwtValidator` (or an injected test fake with the same shape), and
require the ``curator`` scope. No route may accept a caller-supplied user identifier (a query/body/path
``sub``) -- that would let one user act on another's data. Every route keys exclusively off the validated
token's own ``sub``.

:func:`require_verified_caller` layers one more requirement on top for routes that compare the caller's
Identity email against a PSN account's email (link/unlink, ``/me``'s re-verify): a verified Identity email
is mandatory for those, so a token missing the ``email`` claim is rejected outright rather than treated as
an absent-but-fine value.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from curator.token_validation import TokenClaims, TokenError, TokenValidatorLike

_CURATOR_SCOPE = "curator"


def require_bearer(request: Request) -> TokenClaims:
    """Resolve and validate the caller's bearer token, or reject the request.

    :param request: The incoming request (its ``Authorization`` header carries the token).
    :returns: The validated :class:`~curator.token_validation.TokenClaims`.
    :raises fastapi.HTTPException: 401 (with a ``WWW-Authenticate: Bearer`` header), if the header is
        missing/malformed or the token fails validation; 403, if the token is valid but lacks the
        ``curator`` scope.
    """
    token = _extract_bearer_token(request)
    if token is None:
        raise HTTPException(
            status_code=401,
            detail="Bearer token required.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    validator: TokenValidatorLike = request.app.state.token_validator
    try:
        claims: TokenClaims = validator.validate(token)
    except TokenError as exc:
        raise HTTPException(
            status_code=401,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    if not claims.has_scope(_CURATOR_SCOPE):
        raise HTTPException(status_code=403, detail="curator scope required.")

    return claims


def require_verified_caller(claims: TokenClaims = Depends(require_bearer)) -> TokenClaims:
    """Require an authenticated, in-scope caller whose token also carries a verified Identity email.

    :param claims: The caller resolved by :func:`require_bearer`.
    :returns: ``claims``, unchanged.
    :raises fastapi.HTTPException: 403, if ``claims.email`` is absent. The ``curator`` ApiScope is
        configured with an ``email`` user claim, so a token with the ``curator`` scope but no email means
        Identity's own configuration is missing that claim mapping for this user -- not something a route
        that must compare emails can safely proceed without.
    """
    if not claims.email:
        raise HTTPException(status_code=403, detail="email claim required")
    return claims


def _extract_bearer_token(request: Request) -> str | None:
    """Pull the token out of a well-formed ``Authorization: Bearer <token>`` header.

    :param request: The incoming request.
    :returns: The token, or ``None`` if the header is absent or not in the ``Bearer`` scheme.
    """
    header = request.headers.get("Authorization")
    if not header:
        return None

    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token
