from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Annotated, Any, Final, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from curator.audit.recorded import recorded, request_recorder
from curator.audit.repository import ACTION_TROPHY_FETCH
from curator.deps import (
    HARVEST_TROPHIES,
    PREFERENCE_NOT_LINKED_DETAIL,
    PSN_AUTH_FAILED_DETAIL,
    require_bearer,
    require_preference,
)
from curator.psn.errors import PsnAuthError
from curator.psn.identifiers import (
    InvalidPsnIdentifierError,
    validate_np_communication_id,
    validate_trophy_group,
)
from curator.psn.models import (
    ALL_TROPHY_GROUPS,
    TrophyCounts,
    TrophyDetail,
    TrophyGroup,
    TrophyGroups,
    TrophySummary,
    TrophyTitle,
)
from curator.psn.trophy_cache import CachedTrophyClient
from curator.psn.trophy_client import TrophyClient, TrophyClientFactory
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/trophies", tags=["trophies"])

_SUMMARY_DETAIL = "summary"
_TITLES_DETAIL = "titles"

LIMIT_PARAM: Final = "limit"
PLATFORM_PARAM: Final = "platform"
GROUP_PARAM: Final = "group"


class TrophyCountsResponse(BaseModel):
    """A bronze/silver/gold/platinum trophy tally."""

    bronze: int
    silver: int
    gold: int
    platinum: int


class TrophySummaryResponse(BaseModel):
    """The ``GET /trophies/summary`` response body."""

    level: int
    progress: int
    tier: int
    earned: TrophyCountsResponse
    account_id: str | None


class TrophyTitleResponse(BaseModel):
    """One game's trophy status, as returned by ``GET /trophies/titles``."""

    name: str | None
    np_communication_id: str | None
    platforms: list[str]
    progress: int | None
    earned: TrophyCountsResponse
    defined: TrophyCountsResponse
    last_updated: str | None


class TrophyTitlesResponse(BaseModel):
    """The ``GET /trophies/titles`` response body."""

    titles: list[TrophyTitleResponse]


class TrophyDetailResponse(BaseModel):
    """A single trophy's definition merged with the caller's earned progress for it."""

    trophy_id: int | None
    name: str | None
    detail: str | None
    type: str | None
    hidden: bool | None
    icon_url: str | None
    earned: bool | None
    earned_date: str | None
    progress_rate: int | None
    rarity: float | None


class TitleTrophiesResponse(BaseModel):
    """The ``GET /trophies/titles/{np_communication_id}`` response body."""

    trophies: list[TrophyDetailResponse]


class TrophyGroupResponse(BaseModel):
    """One trophy group within a title -- the base game or a single DLC/expansion."""

    group_id: str | None
    name: str | None
    detail: str | None
    icon_url: str | None
    progress: int | None
    defined: TrophyCountsResponse
    earned: TrophyCountsResponse
    last_updated: str | None


class TrophyGroupsResponse(BaseModel):
    """The ``GET /trophies/titles/{np_communication_id}/groups`` response body."""

    title_name: str | None
    platforms: list[str]
    progress: int | None
    defined: TrophyCountsResponse
    earned: TrophyCountsResponse
    groups: list[TrophyGroupResponse]
    last_updated: str | None


@router.get("/summary")
async def get_trophy_summary(
    request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> TrophySummaryResponse:
    """Return the caller's overall trophy standing (level, tier, earned counts).

    :raises fastapi.HTTPException: 404, if the caller has no PSN link; 403, if ``harvest_trophies`` is not
        enabled for this user; 401, if PSN rejects the stored token.
    """
    await require_preference(request, claims.sub, HARVEST_TROPHIES)
    async with recorded(request_recorder(request), claims.sub, ACTION_TROPHY_FETCH, _SUMMARY_DETAIL):
        client = await _trophy_client(request, claims)
        summary = await _call(client.trophy_summary)
    return _summary_response(summary)


@router.get("/titles")
async def get_trophy_titles(
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_bearer)],
    limit: int = Query(default=100, ge=1, le=500, alias=LIMIT_PARAM),
) -> TrophyTitlesResponse:
    """List the caller's games that have trophies, with per-game progress.

    :raises fastapi.HTTPException: 404, if the caller has no PSN link; 403, if ``harvest_trophies`` is not
        enabled for this user; 401, if PSN rejects the stored token.
    """
    await require_preference(request, claims.sub, HARVEST_TROPHIES)
    async with recorded(request_recorder(request), claims.sub, ACTION_TROPHY_FETCH, _TITLES_DETAIL):
        client = await _trophy_client(request, claims)
        titles = await _call(client.trophy_titles, limit=limit)
    return TrophyTitlesResponse(titles=[_title_response(title) for title in titles])


@router.get("/titles/{np_communication_id}")
async def get_title_trophies(
    request: Request,
    np_communication_id: str,
    claims: Annotated[TokenClaims, Depends(require_bearer)],
    platform: str = Query(..., alias=PLATFORM_PARAM),
    group: str = Query(default=ALL_TROPHY_GROUPS, alias=GROUP_PARAM),
) -> TitleTrophiesResponse:
    """List every trophy in a title, merged with the caller's earned progress and rarity.

    :param np_communication_id: The title's ``npCommunicationId`` (from ``GET /trophies/titles``).
    :param platform: The title's platform, e.g. ``"PS5"`` or ``"PS4"`` (also from ``GET /trophies/titles``).
    :raises fastapi.HTTPException: 422, if ``np_communication_id`` or ``group`` is malformed; 404, if the
        caller has no PSN link; 403, if ``harvest_trophies`` is not enabled for this user; 401, if PSN
        rejects the stored token.
    """
    np_communication_id = _valid(validate_np_communication_id, np_communication_id)
    group = _valid(validate_trophy_group, group)
    await require_preference(request, claims.sub, HARVEST_TROPHIES)
    async with recorded(request_recorder(request), claims.sub, ACTION_TROPHY_FETCH, np_communication_id):
        client = await _trophy_client(request, claims)
        trophies = await _call(client.title_trophies, np_communication_id, platform, group=group)
    return TitleTrophiesResponse(trophies=[_detail_response(trophy) for trophy in trophies])


@router.get("/titles/{np_communication_id}/groups")
async def get_trophy_groups(
    request: Request,
    np_communication_id: str,
    claims: Annotated[TokenClaims, Depends(require_bearer)],
    platform: str = Query(..., alias=PLATFORM_PARAM),
) -> TrophyGroupsResponse:
    """Get a title's trophy-group breakdown (base game + each DLC), with the caller's earned progress.

    :param np_communication_id: The title's ``npCommunicationId`` (from ``GET /trophies/titles``).
    :param platform: The title's platform, e.g. ``"PS5"`` or ``"PS4"`` (also from ``GET /trophies/titles``).
    :raises fastapi.HTTPException: 422, if ``np_communication_id`` is malformed; 404, if the caller has no
        PSN link; 403, if ``harvest_trophies`` is not enabled for this user; 401, if PSN rejects the stored
        token.
    """
    np_communication_id = _valid(validate_np_communication_id, np_communication_id)
    await require_preference(request, claims.sub, HARVEST_TROPHIES)
    async with recorded(request_recorder(request), claims.sub, ACTION_TROPHY_FETCH, np_communication_id):
        client = await _trophy_client(request, claims)
        groups = await _call(client.trophy_groups, np_communication_id, platform)
    return _groups_response(groups)


async def _trophy_client(request: Request, claims: TokenClaims) -> TrophyClient | CachedTrophyClient:
    trophy_client_factory: TrophyClientFactory = request.app.state.trophy_client_factory
    try:
        return await trophy_client_factory(claims.sub)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=PREFERENCE_NOT_LINKED_DETAIL) from exc


_T = TypeVar("_T")


def _valid(validator: Callable[[str], str], value: str) -> str:
    try:
        return validator(value)
    except InvalidPsnIdentifierError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _call(method: Callable[..., Coroutine[Any, Any, _T]], *args: Any, **kwargs: Any) -> _T:
    """Invoke a trophy-client method, translating ``PsnAuthError`` to a 401."""
    try:
        return await method(*args, **kwargs)
    except PsnAuthError as exc:
        raise HTTPException(status_code=401, detail=PSN_AUTH_FAILED_DETAIL) from exc


def _counts_response(counts: TrophyCounts) -> TrophyCountsResponse:
    return TrophyCountsResponse(bronze=counts.bronze, silver=counts.silver, gold=counts.gold, platinum=counts.platinum)


def _summary_response(summary: TrophySummary) -> TrophySummaryResponse:
    return TrophySummaryResponse(
        level=summary.level,
        progress=summary.progress,
        tier=summary.tier,
        earned=_counts_response(summary.earned),
        account_id=summary.account_id,
    )


def _title_response(title: TrophyTitle) -> TrophyTitleResponse:
    return TrophyTitleResponse(
        name=title.name,
        np_communication_id=title.np_communication_id,
        platforms=list(title.platforms),
        progress=title.progress,
        earned=_counts_response(title.earned),
        defined=_counts_response(title.defined),
        last_updated=title.last_updated,
    )


def _detail_response(detail: TrophyDetail) -> TrophyDetailResponse:
    return TrophyDetailResponse(
        trophy_id=detail.trophy_id,
        name=detail.name,
        detail=detail.detail,
        type=detail.type,
        hidden=detail.hidden,
        icon_url=detail.icon_url,
        earned=detail.earned,
        earned_date=detail.earned_date,
        progress_rate=detail.progress_rate,
        rarity=detail.rarity,
    )


def _group_response(group: TrophyGroup) -> TrophyGroupResponse:
    return TrophyGroupResponse(
        group_id=group.group_id,
        name=group.name,
        detail=group.detail,
        icon_url=group.icon_url,
        progress=group.progress,
        defined=_counts_response(group.defined),
        earned=_counts_response(group.earned),
        last_updated=group.last_updated,
    )


def _groups_response(groups: TrophyGroups) -> TrophyGroupsResponse:
    return TrophyGroupsResponse(
        title_name=groups.title_name,
        platforms=list(groups.platforms),
        progress=groups.progress,
        defined=_counts_response(groups.defined),
        earned=_counts_response(groups.earned),
        groups=[_group_response(group) for group in groups.groups],
        last_updated=groups.last_updated,
    )
