"""Collapses PSN's per-activation device rows into one entry per device, for every route that reads the
account's registered devices."""

from __future__ import annotations

from dataclasses import replace

from curator.psn.models import AccountDevice


def collapse_by_device_id(devices: list[AccountDevice]) -> list[AccountDevice]:
    """Collapse repeated registrations of one device into a single entry.

    PSN returns one row per *activation*, so a device that was registered more than once (or re-registered
    later) appears several times under the same ``device_id``. The most recent activation wins, and a named
    row is preferred over an unnamed one.

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
