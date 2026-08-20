"""Shape validators for the PSN identifiers a caller can supply."""

from __future__ import annotations

import re

_ACCOUNT_ID = re.compile(r"^[0-9]{1,20}$")
_ONLINE_ID = re.compile(r"^[A-Za-z0-9_-]{3,16}$")
_NP_COMMUNICATION_ID = re.compile(r"^NPWR[0-9]{4,7}_[0-9]{2}$")
_TROPHY_GROUP = re.compile(r"^[A-Za-z0-9]{1,16}$")
_DIRECT_MESSAGE_GROUP_ID = re.compile(r"^~[0-9A-Fa-f]{16}\.[0-9A-Fa-f]{16}$")
_ALLOCATED_GROUP_ID = re.compile(r"^[0-9A-Fa-f]{40}-[0-9]{1,10}$")


class InvalidPsnIdentifierError(ValueError):
    """Raised when a caller-supplied PSN identifier does not match its documented shape."""


def validate_account_id(value: str) -> str:
    """Return ``value``, or raise :class:`InvalidPsnIdentifierError`."""
    return _checked(value, _ACCOUNT_ID, "account id")


def validate_online_id(value: str) -> str:
    """Return ``value``, or raise :class:`InvalidPsnIdentifierError`."""
    return _checked(value, _ONLINE_ID, "online id")


def validate_np_communication_id(value: str) -> str:
    """Return ``value``, or raise :class:`InvalidPsnIdentifierError`."""
    return _checked(value, _NP_COMMUNICATION_ID, "npCommunicationId")


def validate_trophy_group(value: str) -> str:
    """Return ``value``, or raise :class:`InvalidPsnIdentifierError`."""
    return _checked(value, _TROPHY_GROUP, "trophy group")


def validate_group_id(value: str) -> str:
    """Return ``value``, or raise :class:`InvalidPsnIdentifierError`."""
    if _DIRECT_MESSAGE_GROUP_ID.match(value) or _ALLOCATED_GROUP_ID.match(value):
        return value
    raise InvalidPsnIdentifierError("Not a PSN chat group id.")


def _checked(value: str, pattern: re.Pattern[str], label: str) -> str:
    if pattern.match(value):
        return value
    raise InvalidPsnIdentifierError(f"Not a PSN {label}.")
