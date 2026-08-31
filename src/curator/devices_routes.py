from __future__ import annotations

from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.collections.repository import CollectionsRepository
from curator.deps import require_bearer, require_preference
from curator.psn.errors import PsnAuthError
from curator.psn.models import AccountDevice
from curator.psn.social_client import SocialClient, SocialClientFactory
from curator.token_validation import TokenClaims

router = APIRouter(tags=["devices"])

_NO_LINK_DETAIL = "PSN account not linked."
_AUTH_FAILED_DETAIL = "PSN authentication failed; re-link your account."


class AccountDeviceResponse(BaseModel):
    """A single registered console/device, as returned by ``GET /devices``."""

    device_id: str | None
    device_type: str | None
    device_name: str | None
    activation_type: str | None
    activation_date: str | None
    deactivation_date: str | None
    linked_console_id: str | None = None


class DevicesResponse(BaseModel):
    """The ``GET /devices`` response body."""

    devices: list[AccountDeviceResponse]


@router.get("/devices")
async def get_devices(request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]) -> DevicesResponse:
    """Return the consoles/devices registered to the caller's own PSN account.

    :raises fastapi.HTTPException: 404, if the caller has no PSN link; 403, if ``harvest_devices`` is not
        enabled for this user; 401, if PSN rejects the stored token.
    """
    await require_preference(request, claims.sub, "harvest_devices")

    social_client_factory: SocialClientFactory = request.app.state.social_client_factory
    try:
        client: SocialClient = await social_client_factory(claims.sub)
    except RuntimeError as exc:
        raise HTTPException(status_code=404, detail=_NO_LINK_DETAIL) from exc

    try:
        devices = await client.devices()
    except PsnAuthError as exc:
        raise HTTPException(status_code=401, detail=_AUTH_FAILED_DETAIL) from exc

    collections_repository: CollectionsRepository = request.app.state.collections_repository
    console_id_by_device = await collections_repository.list_console_device_links(claims.sub)
    return DevicesResponse(
        devices=[_device_response(device, console_id_by_device) for device in _collapse_by_device_id(devices)]
    )


def _collapse_by_device_id(devices: list[AccountDevice]) -> list[AccountDevice]:
    """Collapse repeated registrations of one device into a single entry.

    PSN returns one row per *activation*, so a device that was registered more than once (or
    re-registered later) appears several times under the same ``device_id``. The most recent activation
    wins, and a named row is preferred over an unnamed one.

    :param devices: Devices exactly as PSN returned them.
    :returns: One entry per ``device_id``, in first-seen order. Entries with no ``device_id`` are kept
        as-is, since nothing identifies them well enough to merge.
    """
    collapsed: dict[str, AccountDevice] = {}
    unidentified: list[AccountDevice] = []
    order: list[str] = []

    for device in devices:
        if not device.device_id:
            unidentified.append(device)
            continue
        existing = collapsed.get(device.device_id)
        if existing is None:
            collapsed[device.device_id] = device
            order.append(device.device_id)
            continue
        collapsed[device.device_id] = _merge_registrations(existing, device)

    return [collapsed[device_id] for device_id in order] + unidentified


def _merge_registrations(existing: AccountDevice, candidate: AccountDevice) -> AccountDevice:
    winner, loser = (
        (candidate, existing)
        if _activation_sort_key(candidate) > _activation_sort_key(existing)
        else (existing, candidate)
    )
    if winner.device_name:
        return winner
    return replace(winner, device_name=loser.device_name) if loser.device_name else winner


def _activation_sort_key(device: AccountDevice) -> str:
    return device.activation_date or ""


def _device_response(device: AccountDevice, console_id_by_device: dict[str, str]) -> AccountDeviceResponse:
    return AccountDeviceResponse(
        device_id=device.device_id,
        device_type=device.device_type,
        device_name=device.device_name,
        activation_type=device.activation_type,
        activation_date=device.activation_date,
        deactivation_date=device.deactivation_date,
        linked_console_id=console_id_by_device.get(device.device_id) if device.device_id else None,
    )
