"""``/me/profile-settings``, ``/users/{sub}/profile``, ``/users/{sub}/follow``,
``/users/{sub}/followers``/``following``, and ``/users/{sub}/library``/``collections`` -- the public
social-profile feature.

``GET /users/{sub}/profile`` (and its follow/library/collections siblings) is a deliberate, narrow
exception to "no caller-supplied target user" (see ``curator.deps``'s module docstring): when viewer B
requests A's public profile, this route builds **B's own** ``TrophyClient``/``SocialClient`` (via
``trophy_client_factory(claims.sub)`` / ``social_client_factory(claims.sub)``) and calls it with
``account_id=A's psn_account_id``. No PSN data ever flows through A's stored token -- B's own live PSN
session looks up A's already-public account, the same way ``SocialClient.friends()``/``.profile()``/
``.friendship()`` already do for arbitrary ``account_id`` targets. If B has no PSN link (or PSN rejects
B's token), PSN sections are simply omitted (not an error) -- B still sees ``psn_account_id``, follow
status, and counts.

Follower/following counts and lists are first-party Curator data (``curator.persistence.follow_repository
.FollowRepository``), not PSN-derived, so unlike the PSN-backed sections they are **never** gated on the
target's ``user_profiles.is_public`` flag -- see ``db/migrations/0006_follows.sql``.

Every ``show_*`` toggle on ``user_profiles`` is meaningless on its own: trophies/identity additionally
require the matching ``psn_links.harvest_*`` flag, and library/collections additionally require
``is_public``. The owner viewing their own profile always sees their own sections (subject to their own
``harvest_*``/``show_*`` settings, not ``is_public`` -- ``is_public`` only controls what *other* viewers
see).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel

from curator.audit.repository import ACTION_FOLLOWED, ACTION_UNFOLLOWED, AccountActionLogRepository
from curator.collections.repository import CollectionsRepository
from curator.deps import require_bearer
from curator.library.repository import LibraryRepository
from curator.persistence.follow_repository import FollowEdge, FollowRepository
from curator.persistence.profile_repository import ProfileRepository, ProfileSettings
from curator.persistence.repository import LinkRecord, Repository
from curator.psn.errors import PsnAuthError
from curator.psn.models import TrophyCounts
from curator.psn.social_client import SocialClientFactory
from curator.psn.trophy_client import TrophyClientFactory
from curator.token_validation import TokenClaims
from curator.trophy_routes import TrophyCountsResponse

router = APIRouter(tags=["profile"])
logger = logging.getLogger("curator")

_USER_NOT_FOUND_DETAIL = "User not found."


class ProfileSettingsResponse(BaseModel):
    """The ``GET/PUT /me/profile-settings`` response body."""

    is_public: bool
    show_library: bool
    show_collections: bool
    show_trophies: bool
    show_identity: bool


class ProfileSettingsRequest(BaseModel):
    """The ``PUT /me/profile-settings`` request body."""

    is_public: bool
    show_library: bool
    show_collections: bool
    show_trophies: bool
    show_identity: bool


class ProfileTrophySummaryResponse(BaseModel):
    """The trophy section of ``GET /users/{sub}/profile``."""

    level: int
    tier: int
    earned: TrophyCountsResponse


class ProfileIdentityResponse(BaseModel):
    """The identity section of ``GET /users/{sub}/profile``.

    No ``region`` field, structurally -- PSN only ever exposes an account's region to that account itself,
    which makes it useless for a cross-user viewer's-own-token lookup like this one (see the module
    docstring). This is a deliberate omission from the response shape, not a value dropped after the fact.
    """

    online_id: str


class PublicProfileResponse(BaseModel):
    """The ``GET /users/{sub}/profile`` response body."""

    sub: str
    psn_account_id: str | None
    is_public: bool
    viewer_is_owner: bool
    viewer_is_following: bool
    follower_count: int
    following_count: int
    library_visible: bool
    collections_visible: bool
    trophies: ProfileTrophySummaryResponse | None
    identity: ProfileIdentityResponse | None


class FollowListEntryResponse(BaseModel):
    """One entry of ``GET /users/{sub}/followers``/``following``."""

    sub: str
    psn_account_id: str | None
    followed_at: str


class FollowListResponse(BaseModel):
    """The ``GET /users/{sub}/followers``/``following`` response body."""

    entries: list[FollowListEntryResponse]
    total: int


class ProfileLibraryGameResponse(BaseModel):
    """One entry of ``GET /users/{sub}/library`` -- same shape as ``curator.library_routes
    .LibraryGameResponse``, the caller's-own equivalent."""

    game_id: str
    title: str
    rawg_enriched: bool
    opencritic_enriched: bool


class ProfileDefinitionResponse(BaseModel):
    """One entry of ``GET /users/{sub}/collections`` -- same shape as ``curator.collections_routes
    .DefinitionResponse``, the caller's-own equivalent, minus the fields only the owner needs to re-run it."""

    definition_id: str
    name: str
    kind: str
    console_id: str | None


@router.get("/me/profile-settings", response_model=ProfileSettingsResponse)
async def get_my_profile_settings(
    request: Request, claims: TokenClaims = Depends(require_bearer)
) -> ProfileSettingsResponse:
    """Return the caller's own profile display-visibility toggles.

    Always answerable -- never 404s, even for a caller who has never visited profile settings.
    """
    profile_repository: ProfileRepository = request.app.state.profile_repository
    settings = await profile_repository.get_settings(claims.sub)
    return _settings_response(settings)


@router.put("/me/profile-settings", response_model=ProfileSettingsResponse)
async def set_my_profile_settings(
    body: ProfileSettingsRequest, request: Request, claims: TokenClaims = Depends(require_bearer)
) -> ProfileSettingsResponse:
    """Set the caller's own profile display-visibility toggles.

    No PSN-link precondition -- these toggles are meaningful even before/without a link.
    """
    profile_repository: ProfileRepository = request.app.state.profile_repository
    await profile_repository.upsert_settings(
        claims.sub,
        is_public=body.is_public,
        show_library=body.show_library,
        show_collections=body.show_collections,
        show_trophies=body.show_trophies,
        show_identity=body.show_identity,
    )
    return ProfileSettingsResponse(
        is_public=body.is_public,
        show_library=body.show_library,
        show_collections=body.show_collections,
        show_trophies=body.show_trophies,
        show_identity=body.show_identity,
    )


@router.get("/users/{sub}/profile", response_model=PublicProfileResponse)
async def get_user_profile(
    sub: str, request: Request, claims: TokenClaims = Depends(require_bearer)
) -> PublicProfileResponse:
    """Return ``sub``'s public profile, as seen by the caller.

    Follow status/counts are never gated by ``is_public`` (see the module docstring). A non-owner viewing
    a private profile still gets ``200`` -- with ``psn_account_id=None``, ``library_visible=False``,
    ``collections_visible=False``, ``trophies=None``, ``identity=None`` -- rather than a 403 or 404, since
    the follow graph and counts are always real and visible regardless.

    :raises fastapi.HTTPException: 404, if ``sub`` has no ``app_users`` row at all.
    """
    repository: Repository = request.app.state.repository
    if not await repository.user_exists(sub):
        raise HTTPException(status_code=404, detail=_USER_NOT_FOUND_DETAIL)

    profile_repository: ProfileRepository = request.app.state.profile_repository
    follow_repository: FollowRepository = request.app.state.follow_repository

    target_settings = await profile_repository.get_settings(sub)
    target_link = await repository.get_link(sub)
    viewer_is_owner = claims.sub == sub
    viewer_can_see_public_sections = viewer_is_owner or target_settings.is_public

    follower_count = await follow_repository.follower_count(sub)
    following_count = await follow_repository.following_count(sub)
    viewer_is_following = await follow_repository.is_following(claims.sub, sub)

    psn_account_id = target_link.psn_account_id if viewer_can_see_public_sections and target_link is not None else None
    library_visible = viewer_is_owner or (target_settings.is_public and target_settings.show_library)
    collections_visible = viewer_is_owner or (target_settings.is_public and target_settings.show_collections)

    trophies = await _cross_user_trophies(request, claims, target_settings, target_link, viewer_can_see_public_sections)
    identity = await _cross_user_identity(request, claims, target_settings, target_link, viewer_can_see_public_sections)

    return PublicProfileResponse(
        sub=sub,
        psn_account_id=psn_account_id,
        is_public=target_settings.is_public,
        viewer_is_owner=viewer_is_owner,
        viewer_is_following=viewer_is_following,
        follower_count=follower_count,
        following_count=following_count,
        library_visible=library_visible,
        collections_visible=collections_visible,
        trophies=trophies,
        identity=identity,
    )


async def _cross_user_trophies(
    request: Request,
    claims: TokenClaims,
    target_settings: ProfileSettings,
    target_link: LinkRecord | None,
    viewer_can_see_public_sections: bool,
) -> ProfileTrophySummaryResponse | None:
    """Build the profile's trophy section using the *viewer's own* ``TrophyClient``, called with the
    *target's* ``account_id`` -- see the module docstring for why this is safe.

    Degrades silently (returns ``None``) rather than raising whenever the section isn't showable for any
    reason: not gated in, the target has no linked account id, the viewer has no PSN link of their own, or
    PSN rejects the viewer's stored token. None of these are the caller's fault, and a page render must
    never 500/403 over another user's PSN state.
    """
    if not (
        viewer_can_see_public_sections
        and target_settings.show_trophies
        and target_link is not None
        and target_link.harvest_trophies
        and target_link.psn_account_id is not None
    ):
        return None

    trophy_client_factory: TrophyClientFactory = request.app.state.trophy_client_factory
    try:
        viewer_client = await trophy_client_factory(claims.sub)
        summary = await viewer_client.trophy_summary(account_id=target_link.psn_account_id)
    except (RuntimeError, PsnAuthError):
        return None
    return ProfileTrophySummaryResponse(level=summary.level, tier=summary.tier, earned=_counts_response(summary.earned))


async def _cross_user_identity(
    request: Request,
    claims: TokenClaims,
    target_settings: ProfileSettings,
    target_link: LinkRecord | None,
    viewer_can_see_public_sections: bool,
) -> ProfileIdentityResponse | None:
    """Build the profile's identity section using the *viewer's own* ``SocialClient``, called with the
    *target's* ``account_id``. Same degrade-silently rationale as :func:`_cross_user_trophies`.

    Not built from ``identity_client_factory``/``AccountClient`` -- ``GET /identity``'s own self-only
    mechanism has no cross-user-by-``account_id`` overload. Only ``SocialClient.online_id()`` and
    ``TrophyClient.trophy_summary(account_id=...)`` support targeting another account; this is a factual
    constraint of the real PSN client code, not a design choice.
    """
    if not (
        viewer_can_see_public_sections
        and target_settings.show_identity
        and target_link is not None
        and target_link.harvest_identity
        and target_link.psn_account_id is not None
    ):
        return None

    social_client_factory: SocialClientFactory = request.app.state.social_client_factory
    try:
        viewer_client = await social_client_factory(claims.sub)
        online_id = await viewer_client.online_id(target_link.psn_account_id)
    except (RuntimeError, PsnAuthError):
        return None
    if online_id is None:
        return None
    return ProfileIdentityResponse(online_id=online_id)


@router.post("/users/{sub}/follow", status_code=204)
async def follow_user(sub: str, request: Request, claims: TokenClaims = Depends(require_bearer)) -> Response:
    """Follow ``sub``.

    Idempotent -- following a user already followed still returns 204. Every call (including a repeat) is
    logged as ``ACTION_FOLLOWED``, matching ``ACTION_TROPHY_FETCH``'s per-request logging precedent.

    :raises fastapi.HTTPException: 404, if ``sub`` has no ``app_users`` row; 400, if ``sub == claims.sub``
        (checked here, before the ``follows_no_self_follow`` CHECK constraint would raise undisguised, for
        a clean error message).
    """
    repository: Repository = request.app.state.repository
    if not await repository.user_exists(sub):
        raise HTTPException(status_code=404, detail=_USER_NOT_FOUND_DETAIL)
    if sub == claims.sub:
        raise HTTPException(status_code=400, detail="Cannot follow yourself.")

    follow_repository: FollowRepository = request.app.state.follow_repository
    await follow_repository.follow(claims.sub, sub)

    await _log(request, claims.sub, ACTION_FOLLOWED, sub)
    return Response(status_code=204)


@router.delete("/users/{sub}/follow", status_code=204)
async def unfollow_user(sub: str, request: Request, claims: TokenClaims = Depends(require_bearer)) -> Response:
    """Unfollow ``sub``.

    Idempotent -- always returns 204, even if ``sub`` wasn't followed (or doesn't exist). ``ACTION_UNFOLLOWED``
    is only logged when a row was actually removed, to avoid audit noise on repeat no-op unfollows.
    """
    follow_repository: FollowRepository = request.app.state.follow_repository
    removed = await follow_repository.unfollow(claims.sub, sub)
    if removed:
        await _log(request, claims.sub, ACTION_UNFOLLOWED, sub)
    return Response(status_code=204)


@router.get("/users/{sub}/followers", response_model=FollowListResponse)
async def get_followers(
    sub: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    claims: TokenClaims = Depends(require_bearer),
) -> FollowListResponse:
    """List the users following ``sub``, newest first. Not gated by ``is_public`` (see the module docstring).

    :raises fastapi.HTTPException: 404, if ``sub`` has no ``app_users`` row.
    """
    repository: Repository = request.app.state.repository
    if not await repository.user_exists(sub):
        raise HTTPException(status_code=404, detail=_USER_NOT_FOUND_DETAIL)

    follow_repository: FollowRepository = request.app.state.follow_repository
    edges = await follow_repository.list_followers(sub, limit=limit, offset=offset)
    total = await follow_repository.follower_count(sub)
    entries = [await _follow_entry(request, edge) for edge in edges]
    return FollowListResponse(entries=entries, total=total)


@router.get("/users/{sub}/following", response_model=FollowListResponse)
async def get_following(
    sub: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    claims: TokenClaims = Depends(require_bearer),
) -> FollowListResponse:
    """List the users ``sub`` follows, newest first. Not gated by ``is_public`` (see the module docstring).

    :raises fastapi.HTTPException: 404, if ``sub`` has no ``app_users`` row.
    """
    repository: Repository = request.app.state.repository
    if not await repository.user_exists(sub):
        raise HTTPException(status_code=404, detail=_USER_NOT_FOUND_DETAIL)

    follow_repository: FollowRepository = request.app.state.follow_repository
    edges = await follow_repository.list_following(sub, limit=limit, offset=offset)
    total = await follow_repository.following_count(sub)
    entries = [await _follow_entry(request, edge) for edge in edges]
    return FollowListResponse(entries=entries, total=total)


async def _follow_entry(request: Request, edge: FollowEdge) -> FollowListEntryResponse:
    """Resolve one follow-list row's ``psn_account_id`` -- shown only if that *other* user's own profile is
    public and linked, never derived from the requesting caller's own visibility."""
    profile_repository: ProfileRepository = request.app.state.profile_repository
    repository: Repository = request.app.state.repository

    psn_account_id: str | None = None
    settings = await profile_repository.get_settings(edge.sub)
    if settings.is_public:
        link = await repository.get_link(edge.sub)
        if link is not None:
            psn_account_id = link.psn_account_id

    return FollowListEntryResponse(
        sub=edge.sub, psn_account_id=psn_account_id, followed_at=edge.followed_at.isoformat()
    )


@router.get("/users/{sub}/library", response_model=list[ProfileLibraryGameResponse])
async def get_user_library(
    sub: str, request: Request, claims: TokenClaims = Depends(require_bearer)
) -> list[ProfileLibraryGameResponse]:
    """Return ``sub``'s library, read-only.

    :raises fastapi.HTTPException: 404, if ``sub`` has no ``app_users`` row. 403, unless the caller is the
        owner or the target's profile is both public and ``show_library``.
    """
    await _require_visible_section(request, sub, claims, "show_library")

    library_repository: LibraryRepository = request.app.state.library_repository
    games = await library_repository.list_entries_with_enrichment(sub)
    return [
        ProfileLibraryGameResponse(
            game_id=game.game_id,
            title=game.title,
            rawg_enriched=game.rawg_enriched,
            opencritic_enriched=game.opencritic_enriched,
        )
        for game in games
    ]


@router.get("/users/{sub}/collections", response_model=list[ProfileDefinitionResponse])
async def get_user_collections(
    sub: str, request: Request, claims: TokenClaims = Depends(require_bearer)
) -> list[ProfileDefinitionResponse]:
    """Return ``sub``'s saved collection definitions, read-only.

    :raises fastapi.HTTPException: 404, if ``sub`` has no ``app_users`` row. 403, unless the caller is the
        owner or the target's profile is both public and ``show_collections``.
    """
    await _require_visible_section(request, sub, claims, "show_collections")

    collections_repository: CollectionsRepository = request.app.state.collections_repository
    definitions = await collections_repository.list_definitions(sub)
    return [
        ProfileDefinitionResponse(
            definition_id=definition.definition_id,
            name=definition.name,
            kind=definition.kind,
            console_id=definition.console_id,
        )
        for definition in definitions
    ]


async def _require_visible_section(request: Request, sub: str, claims: TokenClaims, flag: str) -> None:
    """Shared 404/403 gate for the library/collections passthrough routes.

    :param flag: ``"show_library"`` or ``"show_collections"``.
    :raises fastapi.HTTPException: 404, if ``sub`` has no ``app_users`` row. 403, unless the caller is the
        owner or the target's profile is public with ``flag`` enabled.
    """
    repository: Repository = request.app.state.repository
    if not await repository.user_exists(sub):
        raise HTTPException(status_code=404, detail=_USER_NOT_FOUND_DETAIL)

    if sub == claims.sub:
        return

    profile_repository: ProfileRepository = request.app.state.profile_repository
    settings = await profile_repository.get_settings(sub)
    if not (settings.is_public and getattr(settings, flag)):
        raise HTTPException(status_code=403, detail="This section of the user's profile is not public.")


def _counts_response(counts: TrophyCounts) -> TrophyCountsResponse:
    return TrophyCountsResponse(bronze=counts.bronze, silver=counts.silver, gold=counts.gold, platinum=counts.platinum)


def _settings_response(settings: ProfileSettings) -> ProfileSettingsResponse:
    return ProfileSettingsResponse(
        is_public=settings.is_public,
        show_library=settings.show_library,
        show_collections=settings.show_collections,
        show_trophies=settings.show_trophies,
        show_identity=settings.show_identity,
    )


async def _log(request: Request, sub: str, action: str, detail: str) -> None:
    """Write one audit entry -- the other user's sub only, never PSN data.

    Never lets a logging failure break the user-facing request, matching ``curator.enrichment_keys_routes``
    ``_log`` precedent.
    """
    audit_repository: AccountActionLogRepository = request.app.state.audit_repository
    try:
        await audit_repository.log(sub, action, detail)
    except Exception:
        logger.exception("Failed to write account_action_log entry (sub=%s, action=%s)", sub, action)
