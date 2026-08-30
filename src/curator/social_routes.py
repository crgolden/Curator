from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from curator.audit.repository import (
    ACTION_CHAT_GROUP_CREATED,
    ACTION_CHAT_MEMBERSHIP_CHANGED,
    ACTION_FRIEND_ADDED,
    ACTION_FRIEND_REMOVED,
    AccountActionLogRepository,
)
from curator.deps import require_bearer, require_preference
from curator.psn.errors import MutationNotAllowedError, PsnAuthError
from curator.psn.identifiers import (
    InvalidPsnIdentifierError,
    validate_account_id,
    validate_group_id,
    validate_online_id,
)
from curator.psn.mutation_service import MutationService, MutationServiceFactory
from curator.psn.safety import CHAT_WRITES, FRIEND_WRITES
from curator.token_validation import TokenClaims

router = APIRouter(tags=["social"])

logger = logging.getLogger("curator")

_T = TypeVar("_T")

_NO_LINK_DETAIL = "PSN account not linked."
_AUTH_FAILED_DETAIL = "PSN authentication failed; re-link your account."


class CreateGroupRequest(BaseModel):
    """Body for ``POST /me/chat/groups``."""

    online_ids: list[str] = []
    account_ids: list[str] = []


class CreateGroupResponse(BaseModel):
    """The ``POST /me/chat/groups`` response body."""

    group_id: str | None


@router.put("/me/friends/{online_id}", status_code=204)
async def add_friend(online_id: str, request: Request, claims: TokenClaims = Depends(require_bearer)) -> Response:
    """Send or accept a friend request to ``online_id`` on the caller's own PSN account.

    :raises fastapi.HTTPException: 422, if ``online_id`` is not a PSN online id; 404, if the caller has no
        PSN link; 403, if ``allow_friend_writes`` is not enabled or the mutation cap is spent; 401, if PSN
        rejects the stored token.
    """
    online_id = _valid(validate_online_id, online_id)
    await require_preference(request, claims.sub, FRIEND_WRITES)
    service = await _service(request, claims.sub)

    await _run(service.accept_friend(online_id=online_id))
    await _log(request, claims.sub, ACTION_FRIEND_ADDED, online_id)
    return Response(status_code=204)


@router.delete("/me/friends/{online_id}", status_code=204)
async def remove_friend(online_id: str, request: Request, claims: TokenClaims = Depends(require_bearer)) -> Response:
    """Remove ``online_id`` from the caller's PSN friends, or decline their pending request.

    :raises fastapi.HTTPException: 422, if ``online_id`` is not a PSN online id; 404, if the caller has no
        PSN link; 403, if ``allow_friend_writes`` is not enabled or the mutation cap is spent; 401, if PSN
        rejects the stored token.
    """
    online_id = _valid(validate_online_id, online_id)
    await require_preference(request, claims.sub, FRIEND_WRITES)
    service = await _service(request, claims.sub)

    await _run(service.remove_friend(online_id=online_id))
    await _log(request, claims.sub, ACTION_FRIEND_REMOVED, online_id)
    return Response(status_code=204)


@router.post("/me/chat/groups", response_model=CreateGroupResponse)
async def create_chat_group(
    body: CreateGroupRequest, request: Request, claims: TokenClaims = Depends(require_bearer)
) -> CreateGroupResponse:
    """Create a PSN chat group with the given members, on the caller's own account.

    :raises fastapi.HTTPException: 422, if any member id is malformed; 404, if the caller has no PSN link;
        403, if ``allow_chat_writes`` is not enabled or the mutation cap is spent; 401, if PSN rejects the
        stored token.
    """
    online_ids = [_valid(validate_online_id, member) for member in body.online_ids]
    account_ids = [_valid(validate_account_id, member) for member in body.account_ids]
    await require_preference(request, claims.sub, CHAT_WRITES)
    service = await _service(request, claims.sub)

    group_id = await _run(service.create_group(online_ids=online_ids, account_ids=account_ids))
    await _log(request, claims.sub, ACTION_CHAT_GROUP_CREATED, group_id)
    return CreateGroupResponse(group_id=group_id)


@router.delete("/me/chat/groups/{group_id}/members/me", status_code=204)
async def leave_chat_group(group_id: str, request: Request, claims: TokenClaims = Depends(require_bearer)) -> Response:
    """Leave a PSN chat group. Not reversible from Curator -- rejoining needs an invite.

    :raises fastapi.HTTPException: 422, if ``group_id`` is not a PSN chat group id; 404, if the caller has
        no PSN link; 403, if ``allow_chat_writes`` is not enabled or the mutation cap is spent; 401, if PSN
        rejects the stored token.
    """
    group_id = _valid(validate_group_id, group_id)
    await require_preference(request, claims.sub, CHAT_WRITES)
    service = await _service(request, claims.sub)

    await _run(service.leave_group(group_id))
    await _log(request, claims.sub, ACTION_CHAT_MEMBERSHIP_CHANGED, f"{group_id} left")
    return Response(status_code=204)


def _valid(validator: Callable[[str], str], value: str) -> str:
    try:
        return validator(value)
    except InvalidPsnIdentifierError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _service(request: Request, sub: str) -> MutationService:
    factory: MutationServiceFactory = request.app.state.mutation_service_factory
    try:
        return await factory(sub)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=_NO_LINK_DETAIL) from exc


async def _run(awaitable: Coroutine[Any, Any, _T]) -> _T:
    try:
        return await awaitable
    except MutationNotAllowedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PsnAuthError as exc:
        raise HTTPException(status_code=401, detail=_AUTH_FAILED_DETAIL) from exc


async def _log(request: Request, sub: str, action: str, detail: str | None) -> None:
    audit_repository: AccountActionLogRepository = request.app.state.audit_repository
    try:
        await audit_repository.log(sub, action, detail)
    except Exception:
        logger.exception("Failed to write account_action_log entry (sub=%s, action=%s)", sub, action)
