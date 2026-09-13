from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import Annotated, Any, TypeVar

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from curator.audit.repository import (
    ACTION_CHAT_GROUP_CREATED,
    ACTION_CHAT_GROUP_RENAMED,
    ACTION_CHAT_MEMBERSHIP_CHANGED,
    ACTION_FRIEND_ADDED,
    ACTION_FRIEND_REMOVED,
    ACTION_FRIEND_REQUEST_SENT,
    AccountActionLogRepository,
)
from curator.deps import require_bearer, require_preference
from curator.psn.errors import MutationNotAllowedError, NoPendingFriendRequestError, PsnAuthError
from curator.psn.identifiers import (
    InvalidPsnIdentifierError,
    validate_account_id,
    validate_group_id,
    validate_online_id,
)
from curator.psn.mutation_service import MutationService, MutationServiceFactory
from curator.psn.safety import CHAT_WRITES, FRIEND_WRITES
from curator.psn.social_client import SocialClientFactory
from curator.token_validation import TokenClaims

router = APIRouter(tags=["social"])

logger = logging.getLogger("curator")

_T = TypeVar("_T")

_NO_LINK_DETAIL = "PSN account not linked."
_AUTH_FAILED_DETAIL = "PSN authentication failed; re-link your account."
NO_PENDING_REQUEST_DETAIL = "no_pending_request"
"""``PUT /me/friends/{online_id}``'s 409 detail when the target has not sent the caller a request."""


class CreateGroupRequest(BaseModel):
    """Body for ``POST /me/chat/groups``."""

    online_ids: list[str] = []
    account_ids: list[str] = []


class CreateGroupResponse(BaseModel):
    """The ``POST /me/chat/groups`` response body."""

    group_id: str | None


class InviteToGroupRequest(BaseModel):
    """Body for ``POST /me/chat/groups/{group_id}/invitees``."""

    online_ids: list[str] = []
    account_ids: list[str] = []


class InviteToGroupResponse(BaseModel):
    """The ``POST /me/chat/groups/{group_id}/invitees`` response body: the group that now holds the
    invitees, which differs from the path's ``group_id`` when that group was a two-person DM."""

    group_id: str | None


class RenameGroupRequest(BaseModel):
    """Body for ``PATCH /me/chat/groups/{group_id}``."""

    name: str = Field(min_length=1, max_length=64)


class FriendRequestResponse(BaseModel):
    """One pending inbound friend request."""

    online_id: str | None
    account_id: str


class FriendRequestsResponse(BaseModel):
    """The ``GET /me/friend-requests`` response body."""

    requests: list[FriendRequestResponse]


@router.get("/me/friend-requests")
async def list_friend_requests(
    request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> FriendRequestsResponse:
    """List the friend requests other users have sent the caller. A live proxy; nothing is stored.

    :raises fastapi.HTTPException: 404, if the caller has no PSN link; 403, if ``harvest_identity`` is not
        enabled; 401, if PSN rejects the stored token.
    """
    await require_preference(request, claims.sub, "harvest_identity")
    factory: SocialClientFactory = request.app.state.social_client_factory
    try:
        client = await factory(claims.sub)
        pending = await client.friend_requests()
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=_NO_LINK_DETAIL) from exc
    except PsnAuthError as exc:
        raise HTTPException(status_code=401, detail=_AUTH_FAILED_DETAIL) from exc
    return FriendRequestsResponse(
        requests=[FriendRequestResponse(online_id=user.online_id, account_id=user.account_id) for user in pending]
    )


@router.post("/me/friend-requests/{online_id}", status_code=204)
async def send_friend_request(
    online_id: str, request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> Response:
    """Send a friend request to ``online_id`` from the caller's own PSN account.

    :raises fastapi.HTTPException: 422, if ``online_id`` is not a PSN online id; 404, if the caller has no
        PSN link; 403, if ``allow_friend_writes`` is not enabled or the mutation cap is spent; 401, if PSN
        rejects the stored token.
    """
    online_id = _valid(validate_online_id, online_id)
    await require_preference(request, claims.sub, FRIEND_WRITES)
    service = await _service(request, claims.sub)

    await _run(service.send_friend_request(online_id))
    await _log(request, claims.sub, ACTION_FRIEND_REQUEST_SENT, online_id)
    return Response(status_code=204)


@router.put("/me/friends/{online_id}", status_code=204)
async def accept_friend(
    online_id: str, request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> Response:
    """Accept the friend request ``online_id`` has sent the caller. Never sends one.

    :raises fastapi.HTTPException: 422, if ``online_id`` is not a PSN online id; 404, if the caller has no
        PSN link; 403, if ``allow_friend_writes`` is not enabled or the mutation cap is spent; 409, if that
        user has sent no request, in which case nothing is sent to PSN; 401, if PSN rejects the stored token.
    """
    online_id = _valid(validate_online_id, online_id)
    await require_preference(request, claims.sub, FRIEND_WRITES)
    service = await _service(request, claims.sub)

    try:
        await _run(service.accept_friend_request(online_id))
    except NoPendingFriendRequestError as exc:
        raise HTTPException(status_code=409, detail=NO_PENDING_REQUEST_DETAIL) from exc
    await _log(request, claims.sub, ACTION_FRIEND_ADDED, online_id)
    return Response(status_code=204)


@router.delete("/me/friends/{online_id}", status_code=204)
async def remove_friend(
    online_id: str, request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> Response:
    """Remove ``online_id`` from the caller's PSN friends, or decline their pending request.

    Answers 204 without a PSN write when there is nothing between the two accounts, so a retried call
    cannot act twice.

    :raises fastapi.HTTPException: 422, if ``online_id`` is not a PSN online id; 404, if the caller has no
        PSN link; 403, if ``allow_friend_writes`` is not enabled or the mutation cap is spent; 401, if PSN
        rejects the stored token.
    """
    online_id = _valid(validate_online_id, online_id)
    await require_preference(request, claims.sub, FRIEND_WRITES)
    service = await _service(request, claims.sub)

    if await _run(service.remove_friend(online_id=online_id)):
        await _log(request, claims.sub, ACTION_FRIEND_REMOVED, online_id)
    return Response(status_code=204)


@router.post("/me/chat/groups")
async def create_chat_group(
    body: CreateGroupRequest, request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
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


@router.patch("/me/chat/groups/{group_id}", status_code=204)
async def rename_chat_group(
    group_id: str,
    body: RenameGroupRequest,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_bearer)],
) -> Response:
    """Rename a PSN chat group the caller belongs to. Reversible by renaming again.

    :raises fastapi.HTTPException: 422, if ``group_id`` is not a PSN chat group id or the name is blank;
        404, if the caller has no PSN link; 403, if ``allow_chat_writes`` is not enabled or the mutation cap
        is spent; 401, if PSN rejects the stored token.
    """
    group_id = _valid(validate_group_id, group_id)
    await require_preference(request, claims.sub, CHAT_WRITES)
    service = await _service(request, claims.sub)

    await _run(service.rename_group(group_id, body.name))
    await _log(request, claims.sub, ACTION_CHAT_GROUP_RENAMED, group_id)
    return Response(status_code=204)


@router.post("/me/chat/groups/{group_id}/invitees")
async def invite_to_chat_group(
    group_id: str,
    body: InviteToGroupRequest,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_bearer)],
) -> InviteToGroupResponse:
    """Invite users to a PSN chat group the caller belongs to.

    The response names the group the invitees landed in. Inviting into a two-person DM makes PSN allocate a
    new group for all three members rather than growing the DM, so a client must read ``group_id`` from the
    response, not assume the path's.

    :raises fastapi.HTTPException: 422, if ``group_id`` or any member id is malformed; 404, if the caller
        has no PSN link; 403, if ``allow_chat_writes`` is not enabled or the mutation cap is spent; 401, if
        PSN rejects the stored token.
    """
    group_id = _valid(validate_group_id, group_id)
    online_ids = [_valid(validate_online_id, member) for member in body.online_ids]
    account_ids = [_valid(validate_account_id, member) for member in body.account_ids]
    await require_preference(request, claims.sub, CHAT_WRITES)
    service = await _service(request, claims.sub)

    resulting_group_id = await _run(service.invite_to_group(group_id, online_ids=online_ids, account_ids=account_ids))
    await _log(
        request,
        claims.sub,
        ACTION_CHAT_MEMBERSHIP_CHANGED,
        f"{resulting_group_id or group_id} invited {len(online_ids) + len(account_ids)}",
    )
    return InviteToGroupResponse(group_id=resulting_group_id)


@router.delete("/me/chat/groups/{group_id}/members/me", status_code=204)
async def leave_chat_group(
    group_id: str, request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> Response:
    """Leave a PSN chat group. Not reversible from Curator -- rejoining needs an invite.

    Answers 204 without a PSN write when the caller is not a member, so a retried call cannot act twice.

    :raises fastapi.HTTPException: 422, if ``group_id`` is not a PSN chat group id; 404, if the caller has
        no PSN link; 403, if ``allow_chat_writes`` is not enabled or the mutation cap is spent; 401, if PSN
        rejects the stored token.
    """
    group_id = _valid(validate_group_id, group_id)
    await require_preference(request, claims.sub, CHAT_WRITES)
    service = await _service(request, claims.sub)

    if await _run(service.leave_group(group_id)):
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
