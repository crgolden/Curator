"""A console's own storage never needs a playability check here -- it is always directly playable for both
platforms the console itself supports (that is what "built-in" means). The USB-can't-run-PS5-games rule
only applies to attached swappable storage; see ``curator.storage_devices_routes``.
"""

from __future__ import annotations

from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from curator.collections.console_model_defaults import default_capacity_gb
from curator.collections.repository import CollectionsRepository, UserConsole
from curator.deps import require_bearer
from curator.persistence.repository import Repository
from curator.psn.device_registrations import collapse_by_device_id
from curator.psn.errors import PsnAuthError
from curator.psn.social_client import SocialClientFactory
from curator.psn.title_platform import ConsolePlatform, console_platform, platform_vocabulary_message
from curator.token_validation import TokenClaims

router = APIRouter(prefix="/consoles", tags=["consoles"])

ConsoleDeviceLinkState = Literal["linked", "device_deactivated", "device_missing", "not_checked"]
"""What PSN says about a console's linked device. ``not_checked`` is the explicit state when
``harvest_devices`` is off or PSN could not be asked; it is never an omitted field."""


class ConsoleDeviceLinkResponse(BaseModel):
    """A console's link to a PSN-registered device, and what PSN currently says about that device.

    :param state: ``linked`` when PSN still lists the device with no deactivation date;
        ``device_deactivated`` when it lists it with one; ``device_missing`` when it no longer lists it at
        all; ``not_checked`` when the ``harvest_devices`` preference is off or PSN could not be reached.
        The mapping row itself is never deleted by the server.
    """

    device_id: str
    state: ConsoleDeviceLinkState


class ConsoleRequest(BaseModel):
    """The ``POST /consoles`` request body.

    ``raw_capacity_gb`` is optional -- see :func:`~curator.collections.console_model_defaults
    .default_capacity_gb`. ``model`` is purely informational (drives that default lookup at creation
    time only) and is never validated against a known list; an unrecognized or absent model just means a
    coarser, platform-level default rather than a rejected request.
    """

    name: str
    platform: str
    raw_capacity_gb: float | None = None
    model: str | None = None
    update_buffer_gb: float = 0.0
    routing_genres: list[str] = []
    fill_order: int = 0


class ConsoleUpdateRequest(BaseModel):
    """The ``PATCH /consoles/{console_id}`` request body -- every field optional, ``None`` leaves it
    unchanged. ``platform`` is intentionally absent: a console's platform never changes after creation."""

    name: str | None = None
    raw_capacity_gb: float | None = None
    update_buffer_gb: float | None = None
    routing_genres: list[str] | None = None
    fill_order: int | None = None


class ConsoleResponse(BaseModel):
    """One console, including its derived usable capacity.

    :param capacity_is_default: ``True`` only in the response to the ``POST`` that created this console,
        when ``raw_capacity_gb`` was omitted and this capacity was auto-assigned rather than supplied --
        never persisted, never present on ``GET``/``PATCH``/``LIST`` (by then the number is just the
        console's real recorded capacity, defaulted or not, with no distinction left to flag). A client
        uses this one-time signal to show "we guessed this, please correct it if wrong" rather than
        presenting a guess as a confirmed fact.
    :param device_link: The console's PSN device link and its live state, populated by ``GET /consoles``
        only; ``None`` when the console is linked to no device, and on every other route.
    """

    console_id: str
    name: str
    platform: str
    raw_capacity_gb: float
    model: str | None
    update_buffer_gb: float
    effective_capacity_gb: float
    routing_genres: list[str]
    fill_order: int
    capacity_is_default: bool = False
    device_link: ConsoleDeviceLinkResponse | None = None


class ConsoleInstallRequest(BaseModel):
    """The ``PUT /consoles/{console_id}/installs/{game_id}`` request body."""

    installed: bool


class ConsoleInstallResponse(BaseModel):
    """The ``PUT /consoles/{console_id}/installs/{game_id}`` response body."""

    console_id: str
    game_id: str
    installed: bool


class ConsoleInstallsResponse(BaseModel):
    """The ``GET /consoles/{console_id}/installs`` response body -- every game id currently marked
    installed on this console's own built-in storage, for Librarian to hydrate its install toggle from
    instead of relying on session-only state."""

    game_ids: list[str]


def _console_platform(value: str) -> ConsolePlatform:
    try:
        return console_platform(value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=platform_vocabulary_message()) from exc


def _to_response(
    console: UserConsole,
    *,
    capacity_is_default: bool = False,
    device_link: ConsoleDeviceLinkResponse | None = None,
) -> ConsoleResponse:
    return ConsoleResponse(
        console_id=console.console_id,
        name=console.name,
        platform=console.platform,
        raw_capacity_gb=console.raw_capacity_gb,
        model=console.model,
        update_buffer_gb=console.update_buffer_gb,
        effective_capacity_gb=console.effective_capacity_gb,
        routing_genres=list(console.routing_genres),
        fill_order=console.fill_order,
        capacity_is_default=capacity_is_default,
        device_link=device_link,
    )


async def _device_link_states(
    request: Request, sub: str, device_id_by_console: dict[str, str]
) -> dict[str, ConsoleDeviceLinkResponse]:
    """Resolve each linked console's device state, asking PSN only when the caller allows it.

    Zero PSN calls when nothing is linked or ``harvest_devices`` is off; a PSN failure of any kind
    reports ``not_checked`` rather than failing the console list.
    """
    if not device_id_by_console:
        return {}

    def unchecked() -> dict[str, ConsoleDeviceLinkResponse]:
        return {
            console_id: ConsoleDeviceLinkResponse(device_id=device_id, state="not_checked")
            for console_id, device_id in device_id_by_console.items()
        }

    repository: Repository = request.app.state.repository
    link = await repository.get_link(sub)
    if link is None or link.harvest_devices is not True:
        return unchecked()

    social_client_factory: SocialClientFactory = request.app.state.social_client_factory
    try:
        client = await social_client_factory(sub)
        devices = collapse_by_device_id(await client.devices())
    except (RuntimeError, PsnAuthError, httpx.HTTPError):
        return unchecked()

    listed = {device.device_id: device for device in devices if device.device_id}
    states: dict[str, ConsoleDeviceLinkResponse] = {}
    for console_id, device_id in device_id_by_console.items():
        device = listed.get(device_id)
        state: ConsoleDeviceLinkState
        if device is None:
            state = "device_missing"
        elif device.deactivation_date:
            state = "device_deactivated"
        else:
            state = "linked"
        states[console_id] = ConsoleDeviceLinkResponse(device_id=device_id, state=state)
    return states


@router.post("", status_code=201)
async def create_console(
    request: Request, body: ConsoleRequest, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> ConsoleResponse:
    """Create a console for the caller.

    If ``raw_capacity_gb`` is omitted, it's auto-assigned from ``model`` (or a coarser platform-level
    default if ``model`` is absent/unrecognized) -- never refused for missing capacity, only flagged via
    ``capacity_is_default`` in the response so the caller can prompt for a correction rather than silently
    trusting a guess.

    :raises fastapi.HTTPException: 400, if ``platform`` is outside
        :data:`~curator.psn.title_platform.CONSOLE_PLATFORM_IDS` (``user_consoles.platform``'s own foreign
        key to ``platforms`` would reject it anyway; validating here first gives a clearer message than a
        raw constraint-violation 500).
    """
    platform = _console_platform(body.platform)

    capacity_is_default = body.raw_capacity_gb is None
    raw_capacity_gb = body.raw_capacity_gb
    if raw_capacity_gb is None:
        raw_capacity_gb, _matched_model = default_capacity_gb(body.platform, body.model)

    repository: CollectionsRepository = request.app.state.collections_repository
    console = await repository.create_console(
        claims.sub,
        name=body.name,
        platform=platform,
        raw_capacity_gb=raw_capacity_gb,
        update_buffer_gb=body.update_buffer_gb,
        routing_genres=tuple(body.routing_genres),
        fill_order=body.fill_order,
        model=body.model,
    )
    return _to_response(console, capacity_is_default=capacity_is_default)


@router.get("")
async def list_consoles(
    request: Request, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> list[ConsoleResponse]:
    """List every console the caller owns, ordered by ``fill_order``, each with its device link's state.

    PSN is asked about the linked devices only when at least one console is linked and the caller's
    ``harvest_devices`` preference is on; otherwise every link reports ``not_checked``.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    consoles = await repository.list_user_consoles(claims.sub)
    console_id_by_device = await repository.list_console_device_links(claims.sub)
    device_id_by_console = {console_id: device_id for device_id, console_id in console_id_by_device.items()}
    states = await _device_link_states(request, claims.sub, device_id_by_console)
    return [_to_response(console, device_link=states.get(console.console_id)) for console in consoles]


@router.get("/{console_id}")
async def get_console(
    request: Request, console_id: str, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> ConsoleResponse:
    """Read one console.

    :raises fastapi.HTTPException: 404, if ``console_id`` doesn't belong to the caller.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    console = await repository.get_console(claims.sub, console_id)
    if console is None:
        raise HTTPException(status_code=404, detail="Console not found.")
    return _to_response(console)


@router.patch("/{console_id}")
async def update_console(
    request: Request,
    console_id: str,
    body: ConsoleUpdateRequest,
    claims: Annotated[TokenClaims, Depends(require_bearer)],
) -> ConsoleResponse:
    """Patch a console's editable fields.

    :raises fastapi.HTTPException: 404, if ``console_id`` doesn't belong to the caller.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    console = await repository.update_console(
        claims.sub,
        console_id,
        name=body.name,
        raw_capacity_gb=body.raw_capacity_gb,
        update_buffer_gb=body.update_buffer_gb,
        routing_genres=None if body.routing_genres is None else tuple(body.routing_genres),
        fill_order=body.fill_order,
    )
    if console is None:
        raise HTTPException(status_code=404, detail="Console not found.")
    return _to_response(console)


@router.delete("/{console_id}", status_code=204)
async def delete_console(
    request: Request, console_id: str, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> None:
    """Delete a console. Cascades to its own install rows; any attached storage device is detached, not
    deleted -- see ``CollectionsRepository.delete_console``.

    :raises fastapi.HTTPException: 404, if ``console_id`` doesn't belong to the caller.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    deleted = await repository.delete_console(claims.sub, console_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Console not found.")


class DeviceLinkRequest(BaseModel):
    """Body for ``PUT /consoles/{console_id}/device-link``."""

    device_id: str


@router.put("/{console_id}/device-link", status_code=204)
async def link_console_device(
    request: Request, console_id: str, body: DeviceLinkRequest, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> None:
    """Link this console to one of the caller's PSN-registered devices (see ``GET /devices``).

    Re-linking replaces whatever either side pointed at before. ``device_id`` is not validated against PSN.

    :raises fastapi.HTTPException: 404, if ``console_id`` doesn't belong to the caller.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    if await repository.get_console(claims.sub, console_id) is None:
        raise HTTPException(status_code=404, detail="Console not found.")
    await repository.link_console_device(claims.sub, console_id, body.device_id)


@router.delete("/{console_id}/device-link", status_code=204)
async def unlink_console_device(
    request: Request, console_id: str, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> None:
    """Remove this console's device link, leaving both the console and the PSN device untouched.

    :raises fastapi.HTTPException: 404, if ``console_id`` doesn't belong to the caller or has no link.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    if await repository.get_console(claims.sub, console_id) is None:
        raise HTTPException(status_code=404, detail="Console not found.")
    if not await repository.unlink_console_device(claims.sub, console_id):
        raise HTTPException(status_code=404, detail="Console is not linked to a device.")


@router.get("/{console_id}/installs")
async def get_console_installs(
    request: Request, console_id: str, claims: Annotated[TokenClaims, Depends(require_bearer)]
) -> ConsoleInstallsResponse:
    """Every game id currently marked installed on this console's own built-in storage.

    :raises fastapi.HTTPException: 404, if ``console_id`` doesn't belong to the caller.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    if await repository.get_console(claims.sub, console_id) is None:
        raise HTTPException(status_code=404, detail="Console not found.")
    game_ids = await repository.list_installed_game_ids(console_id)
    return ConsoleInstallsResponse(game_ids=sorted(game_ids))


@router.put("/{console_id}/installs/{game_id}")
async def set_console_install(
    request: Request,
    console_id: str,
    game_id: str,
    body: ConsoleInstallRequest,
    claims: Annotated[TokenClaims, Depends(require_bearer)],
) -> ConsoleInstallResponse:
    """Set a game's current install state on a specific console's own built-in storage.

    :returns: The state just recorded.
    :raises fastapi.HTTPException: 404, if ``console_id`` doesn't belong to the caller -- ``console_id`` is
        a path parameter, but ownership is always re-checked against the caller's own token rather than
        trusted from the URL, so one user can never set install state on another user's console.
    """
    repository: CollectionsRepository = request.app.state.collections_repository
    if await repository.get_console(claims.sub, console_id) is None:
        raise HTTPException(status_code=404, detail="Console not found.")

    await repository.set_console_install(console_id, game_id, body.installed)
    return ConsoleInstallResponse(console_id=console_id, game_id=game_id, installed=body.installed)
