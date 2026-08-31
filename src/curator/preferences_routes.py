from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.deps import require_bearer
from curator.library.repository import LibraryRepository
from curator.persistence.repository import LinkRecord, Repository
from curator.token_validation import TokenClaims

router = APIRouter(tags=["preferences"])

_NO_LINK_DETAIL = "PSN account not linked."


class PsnPreferences(BaseModel):
    """The caller's PSN capability opt-in flags: four harvest categories and two write categories.

    The write flags default to ``False`` so an existing client that omits them cannot silently enable a
    state-changing capability on PSN.
    """

    harvest_trophies: bool
    harvest_identity: bool
    harvest_presence: bool
    harvest_devices: bool
    allow_friend_writes: bool = False
    allow_chat_writes: bool = False


@router.get("/me/psn-preferences")
async def get_psn_preferences(
    request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> PsnPreferences:
    """Return the caller's current PSN data-harvest preferences.

    :raises fastapi.HTTPException: 404, if the caller has no PSN link.
    """
    repository: Repository = request.app.state.repository
    link = await repository.get_link(claims.sub)
    if link is None:
        raise HTTPException(status_code=404, detail=_NO_LINK_DETAIL)
    return _response(link)


@router.put("/me/psn-preferences")
async def set_psn_preferences(
    body: PsnPreferences,
    request: Request,
    claims: Annotated[TokenClaims, Depends(require_bearer)],
) -> PsnPreferences:
    """Set the caller's PSN data-harvest preferences (all four flags, in one call).

    :raises fastapi.HTTPException: 404, if the caller has no PSN link.
    """
    repository: Repository = request.app.state.repository
    link = await repository.get_link(claims.sub)
    if link is None:
        raise HTTPException(status_code=404, detail=_NO_LINK_DETAIL)

    await repository.set_psn_preferences(
        claims.sub,
        harvest_trophies=body.harvest_trophies,
        harvest_identity=body.harvest_identity,
        harvest_presence=body.harvest_presence,
        harvest_devices=body.harvest_devices,
        allow_friend_writes=body.allow_friend_writes,
        allow_chat_writes=body.allow_chat_writes,
    )

    if link.harvest_trophies and not body.harvest_trophies:
        library_repository: LibraryRepository = request.app.state.library_repository
        await library_repository.clear_trophy_progress(claims.sub)

    return body


def _response(link: LinkRecord) -> PsnPreferences:
    return PsnPreferences(
        harvest_trophies=link.harvest_trophies,
        harvest_identity=link.harvest_identity,
        harvest_presence=link.harvest_presence,
        harvest_devices=link.harvest_devices,
        allow_friend_writes=link.allow_friend_writes,
        allow_chat_writes=link.allow_chat_writes,
    )
