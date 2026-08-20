"""``POST/GET/PATCH/DELETE /storage-devices`` (swappable M.2/USB storage CRUD),
``PUT /storage-devices/{device_id}/attach`` / ``DELETE .../attach`` (moving a device between consoles),
and ``PUT /storage-devices/{device_id}/installs/{game_id}`` -- the one and only place a device's own
install-checked-state changes.

A storage device is deliberately distinct from a console (``curator.consoles_routes``): it is swappable,
it can be unattached, and moving it to a different console is a one-field update here rather than a bulk
rewrite of every install row it carries -- see ``storage_device_installs`` in migration ``0017`` for why.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.collections.repository import (
    CollectionsRepository,
    StorageDevice,
    StorageDeviceKind,
    storage_device_kind,
)
from curator.deps import require_bearer
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/storage-devices", tags=["storage-devices"])


class StorageDeviceRequest(BaseModel):
    """The ``POST /storage-devices`` request body."""

    name: str
    kind: str
    capacity_gb: float
    buffer_gb: float = 0.0
    console_id: str | None = None


class StorageDeviceUpdateRequest(BaseModel):
    """The ``PATCH /storage-devices/{device_id}`` request body -- every field optional, ``None`` leaves it
    unchanged. ``kind`` is intentionally absent (fixed at creation, matching a console's ``platform``);
    ``console_id`` is intentionally absent -- see the ``attach``/``detach`` routes below, the only place
    attachment changes."""

    name: str | None = None
    capacity_gb: float | None = None
    buffer_gb: float | None = None


class StorageDeviceResponse(BaseModel):
    """One storage device, including its derived usable capacity."""

    device_id: str
    console_id: str | None
    name: str
    kind: str
    capacity_gb: float
    buffer_gb: float
    effective_capacity_gb: float


class StorageDeviceInstallRequest(BaseModel):
    """The ``PUT /storage-devices/{device_id}/installs/{game_id}`` request body."""

    installed: bool


class StorageDeviceInstallResponse(BaseModel):
    """The ``PUT /storage-devices/{device_id}/installs/{game_id}`` response body."""

    device_id: str
    game_id: str
    installed: bool


class StorageDeviceInstallsResponse(BaseModel):
    """The ``GET /storage-devices/{device_id}/installs`` response body -- every game id currently marked
    installed on this device."""

    game_ids: list[str]


def _to_response(device: StorageDevice) -> StorageDeviceResponse:
    return StorageDeviceResponse(
        device_id=device.device_id,
        console_id=device.console_id,
        name=device.name,
        kind=device.kind,
        capacity_gb=device.capacity_gb,
        buffer_gb=device.buffer_gb,
        effective_capacity_gb=device.effective_capacity_gb,
    )


def _storage_device_kind(value: str) -> StorageDeviceKind:
    try:
        return storage_device_kind(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail='kind must be "m2" or "usb".') from exc


async def _require_owned_console(repository: CollectionsRepository, identity_sub: str, console_id: str) -> None:
    """:raises fastapi.HTTPException: 400, if ``console_id`` doesn't belong to the caller -- matches how
    ``collections_routes.save_collection`` validates a collection's own ``console_id`` before it can be
    used, rather than trusting a foreign key alone to surface the mistake as an opaque 500."""
    if await repository.get_console(identity_sub, console_id) is None:
        raise HTTPException(status_code=400, detail=f"Unknown console_id {console_id!r} for this user.")


@router.post("", response_model=StorageDeviceResponse, status_code=201)
async def create_storage_device(
    request: Request, body: StorageDeviceRequest, claims: TokenClaims = Depends(require_bearer)
) -> StorageDeviceResponse:
    """Create a storage device for the caller, optionally already attached to one of their consoles.

    :raises fastapi.HTTPException: 400, if ``kind`` isn't ``"m2"``/``"usb"``, or ``console_id`` is given
        but isn't one of the caller's own consoles.
    """
    kind = _storage_device_kind(body.kind)
    repository: CollectionsRepository = request.app.state.collections_repository
    if body.console_id is not None:
        await _require_owned_console(repository, claims.sub, body.console_id)
    device = await repository.create_storage_device(
        claims.sub,
        name=body.name,
        kind=kind,
        capacity_gb=body.capacity_gb,
        buffer_gb=body.buffer_gb,
        console_id=body.console_id,
    )
    return _to_response(device)


@router.get("", response_model=list[StorageDeviceResponse])
async def list_storage_devices(
    request: Request, claims: TokenClaims = Depends(require_bearer)
) -> list[StorageDeviceResponse]:
    """List every storage device the caller owns, attached or not."""
    repository: CollectionsRepository = request.app.state.collections_repository
    devices = await repository.list_storage_devices(claims.sub)
    return [_to_response(device) for device in devices]


@router.get("/{device_id}", response_model=StorageDeviceResponse)
async def get_storage_device(
    request: Request, device_id: str, claims: TokenClaims = Depends(require_bearer)
) -> StorageDeviceResponse:
    """Read one storage device.

    :raises fastapi.HTTPException: 404, if ``device_id`` doesn't belong to the caller.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    device = await repository.get_storage_device(claims.sub, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Storage device not found.")
    return _to_response(device)


@router.patch("/{device_id}", response_model=StorageDeviceResponse)
async def update_storage_device(
    request: Request,
    device_id: str,
    body: StorageDeviceUpdateRequest,
    claims: TokenClaims = Depends(require_bearer),
) -> StorageDeviceResponse:
    """Patch a device's editable fields.

    :raises fastapi.HTTPException: 404, if ``device_id`` doesn't belong to the caller.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    device = await repository.update_storage_device(
        claims.sub, device_id, name=body.name, capacity_gb=body.capacity_gb, buffer_gb=body.buffer_gb
    )
    if device is None:
        raise HTTPException(status_code=404, detail="Storage device not found.")
    return _to_response(device)


@router.delete("/{device_id}", status_code=204)
async def delete_storage_device(
    request: Request, device_id: str, claims: TokenClaims = Depends(require_bearer)
) -> None:
    """Delete a storage device (cascades to its own install rows).

    :raises fastapi.HTTPException: 404, if ``device_id`` doesn't belong to the caller.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    deleted = await repository.delete_storage_device(claims.sub, device_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Storage device not found.")


@router.put("/{device_id}/attach/{console_id}", response_model=StorageDeviceResponse)
async def attach_storage_device(
    request: Request, device_id: str, console_id: str, claims: TokenClaims = Depends(require_bearer)
) -> StorageDeviceResponse:
    """Attach a device to a console -- e.g. plugging a USB drive or an M.2 expansion into it. Its
    existing installs travel with it (see the module docstring); nothing else changes.

    :raises fastapi.HTTPException: 404, if ``device_id`` doesn't belong to the caller. 400, if
        ``console_id`` isn't one of the caller's own consoles.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    if await repository.get_storage_device(claims.sub, device_id) is None:
        raise HTTPException(status_code=404, detail="Storage device not found.")
    await _require_owned_console(repository, claims.sub, console_id)
    device = await repository.set_storage_device_attachment(claims.sub, device_id, console_id)
    assert device is not None
    return _to_response(device)


@router.delete("/{device_id}/attach", response_model=StorageDeviceResponse)
async def detach_storage_device(
    request: Request, device_id: str, claims: TokenClaims = Depends(require_bearer)
) -> StorageDeviceResponse:
    """Detach a device from whichever console it's currently attached to -- e.g. unplugging a USB drive.
    Its installs stay recorded on the device itself; they simply aren't attributed to any console until
    it's re-attached (see the module docstring).

    :raises fastapi.HTTPException: 404, if ``device_id`` doesn't belong to the caller.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    if await repository.get_storage_device(claims.sub, device_id) is None:
        raise HTTPException(status_code=404, detail="Storage device not found.")
    device = await repository.set_storage_device_attachment(claims.sub, device_id, None)
    assert device is not None
    return _to_response(device)


@router.get("/{device_id}/installs", response_model=StorageDeviceInstallsResponse)
async def get_storage_device_installs(
    request: Request, device_id: str, claims: TokenClaims = Depends(require_bearer)
) -> StorageDeviceInstallsResponse:
    """Every game id currently marked installed on this device.

    :raises fastapi.HTTPException: 404, if ``device_id`` doesn't belong to the caller.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    if await repository.get_storage_device(claims.sub, device_id) is None:
        raise HTTPException(status_code=404, detail="Storage device not found.")
    game_ids = await repository.list_storage_device_installed_game_ids(device_id)
    return StorageDeviceInstallsResponse(game_ids=sorted(game_ids))


@router.put("/{device_id}/installs/{game_id}", response_model=StorageDeviceInstallResponse)
async def set_storage_device_install(
    request: Request,
    device_id: str,
    game_id: str,
    body: StorageDeviceInstallRequest,
    claims: TokenClaims = Depends(require_bearer),
) -> StorageDeviceInstallResponse:
    """Set a game's current install state on a specific storage device.

    Marking a PS5-native game installed on ``kind="usb"`` storage is allowed -- Sony's own Extended
    Storage feature lets a PS5 title be copied to USB, it just can't *run* from there without first being
    moved back to the console's own drive or an M.2 expansion. That's exactly the case this endpoint
    needs to represent: a game parked on USB, taking up that drive's space, not currently playable. The
    one place this distinction *is* enforced is a ``capacity_fill`` collection run
    (``curator.collections.collection_orchestrator``), which never proposes a USB bin for a PS5 title in
    the first place -- a run's job is producing a playable set, not just a storage record.

    :returns: The state just recorded.
    :raises fastapi.HTTPException: 404, if ``device_id`` doesn't belong to the caller.
    """
    collections_repository: CollectionsRepository = request.app.state.collections_repository
    device = await collections_repository.get_storage_device(claims.sub, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Storage device not found.")

    await collections_repository.set_storage_device_install(device_id, game_id, body.installed)
    return StorageDeviceInstallResponse(device_id=device_id, game_id=game_id, installed=body.installed)
