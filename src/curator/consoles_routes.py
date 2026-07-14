"""``PUT /consoles/{console_id}/installs/{game_id}`` -- the one and only place install-checked-state
changes.

Deliberately never a side effect of a collection run: "physically installed here" and "currently
recommended here" stay two distinct facts, so checked state never silently auto-transfers when a game is
reassigned to a different console or collection.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.collections.repository import CollectionsRepository
from curator.deps import require_bearer
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/consoles", tags=["consoles"])


class ConsoleInstallRequest(BaseModel):
    """The ``PUT /consoles/{console_id}/installs/{game_id}`` request body."""

    installed: bool


class ConsoleInstallResponse(BaseModel):
    """The ``PUT /consoles/{console_id}/installs/{game_id}`` response body."""

    console_id: str
    game_id: str
    installed: bool


@router.put("/{console_id}/installs/{game_id}", response_model=ConsoleInstallResponse)
async def set_console_install(
    request: Request,
    console_id: str,
    game_id: str,
    body: ConsoleInstallRequest,
    claims: TokenClaims = Depends(require_bearer),
) -> ConsoleInstallResponse:
    """Set a game's current install state on a specific console.

    :returns: The state just recorded.
    :raises fastapi.HTTPException: 404, if ``console_id`` doesn't belong to the caller -- ``console_id`` is
        a path parameter, but ownership is always re-checked against the caller's own token rather than
        trusted from the URL, so one user can never set install state on another user's console.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    owned_console_ids = {console.console_id for console in await repository.list_user_consoles(claims.sub)}
    if console_id not in owned_console_ids:
        raise HTTPException(status_code=404, detail="Console not found.")

    await repository.set_console_install(console_id, game_id, body.installed)
    return ConsoleInstallResponse(console_id=console_id, game_id=game_id, installed=body.installed)
