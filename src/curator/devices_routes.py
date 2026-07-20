"""``GET /devices`` -- the consoles/devices registered to the caller's own PSN account.

Gated on the caller's own ``harvest_devices`` preference via ``curator.deps.require_preference`` -- see
that function's docstring and ``curator.trophy_routes``'s module docstring for the shared no-link-is-404 /
PsnAuthError-is-401 pattern every PSN-data route in this app follows.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

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


class DevicesResponse(BaseModel):
    """The ``GET /devices`` response body."""

    devices: list[AccountDeviceResponse]


@router.get("/devices", response_model=DevicesResponse)
async def get_devices(request: Request, claims: TokenClaims = Depends(require_bearer)) -> DevicesResponse:
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

    return DevicesResponse(devices=[_device_response(device) for device in devices])


def _device_response(device: AccountDevice) -> AccountDeviceResponse:
    return AccountDeviceResponse(
        device_id=device.device_id,
        device_type=device.device_type,
        device_name=device.device_name,
        activation_type=device.activation_type,
        activation_date=device.activation_date,
        deactivation_date=device.deactivation_date,
    )
