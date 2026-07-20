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

:func:`require_bearer` also guarantees the caller's ``app_users`` row exists before the route body runs.
Every other account table (``psn_links``, and every catalog/collections/library/job-run table) declares a
``REFERENCES app_users (identity_sub)`` foreign key, so any write keyed by a ``sub`` that has never been
upserted fails at the database with a foreign-key violation rather than a clean application error. Doing
this once, here, in the single dependency every protected route already depends on, is what makes that
invariant hold everywhere instead of requiring each route/service to remember it independently.

``curator.profile_routes`` is the first (and, as of this writing, only) place a caller-supplied path
parameter names *another user's account* rather than a game/console/title resource -- ``GET
/users/{sub}/profile`` and its follow/library/collections siblings take a target ``sub`` on purpose, a
deliberate, narrow exception to the "no caller-supplied target user" rule stated above. Every one of those
routes still depends on :func:`require_bearer` for the *caller's own* identity; ``sub`` only ever selects
which profile/follow-graph/library/collections to read or mutate, gated by that profile's own visibility
settings -- see that module's docstring for the full exception, including the cross-user PSN lookup it
performs using the *viewer's* own stored PSN session.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from curator.persistence.repository import LinkRecord, Repository
from curator.token_validation import TokenClaims, TokenError, TokenValidatorLike

_CURATOR_SCOPE = "curator"

_HARVEST_CATEGORIES = {"harvest_trophies", "harvest_identity", "harvest_presence", "harvest_devices"}


async def require_bearer(request: Request) -> TokenClaims:
    """Resolve and validate the caller's bearer token, or reject the request.

    On success, also upserts the caller's ``app_users`` row and stamps ``last_login_at`` -- see the module
    docstring for why this must happen here rather than in an individual route/service.

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

    repository: Repository = request.app.state.repository
    await repository.upsert_user(claims.sub)
    await repository.touch_login(claims.sub)

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


def require_admin(claims: TokenClaims = Depends(require_bearer)) -> TokenClaims:
    """Require an authenticated, in-scope caller whose token also carries the ``curator.admin`` claim.

    Mirrors the ``Directory`` API's ``ChurchesMod`` elevated-claim pattern: the plain ``curator`` scope
    every authenticated user has is not enough to trigger a global catalog re-enrichment run.

    :param claims: The caller resolved by :func:`require_bearer`.
    :returns: ``claims``, unchanged.
    :raises fastapi.HTTPException: 403, if ``claims.is_admin`` is ``False``.
    """
    if not claims.is_admin:
        raise HTTPException(status_code=403, detail="curator.admin claim required.")
    return claims


async def require_preference(request: Request, sub: str, category: str) -> LinkRecord:
    """Require that ``sub`` has a PSN link with the named data-harvest ``category`` flag enabled.

    Called from inside a route handler body (not as a nested ``Depends``) once the caller is already
    resolved via :func:`require_bearer` -- mirrors how ``curator.trophy_routes`` does its own inline
    link/auth checks rather than layering another FastAPI dependency on top.

    :param request: The incoming request (used to reach ``request.app.state.repository``).
    :param sub: The Identity ``sub`` claim of the caller whose preference is being checked.
    :param category: One of ``"harvest_trophies"``, ``"harvest_identity"``, ``"harvest_presence"``,
        ``"harvest_devices"``.
    :returns: The caller's :class:`~curator.persistence.repository.LinkRecord`.
    :raises fastapi.HTTPException: 404, if the caller has no PSN link; 403, if the named category flag is
        not enabled for this user.
    """
    assert category in _HARVEST_CATEGORIES, f"unknown harvest category: {category!r}"

    repository: Repository = request.app.state.repository
    link = await repository.get_link(sub)
    if link is None:
        raise HTTPException(status_code=404, detail="PSN account not linked.")

    if getattr(link, category) is not True:
        raise HTTPException(status_code=403, detail=f"PSN data category '{category}' is not enabled for this user")

    return link


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
