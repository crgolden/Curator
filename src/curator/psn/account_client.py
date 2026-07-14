"""Async client for PSN account identity and email.

Ported from ``psnpy.client.PsnAgent``'s ``whoami()``/``account_email()``/``account_email_verified()`` --
the account-facing slice already partially used by ``curator.link_service`` (via the transitional
``psnpy``-backed adapter in ``app.py``, removed once callers switch to this client).
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

import pycountry

from curator.psn.session import PsnSession

_PROFILE_URI = "https://m.np.playstation.com/api/userProfile/v1/internal/users"
_LEGACY_PROFILE_URI = "https://us-prof.np.community.playstation.net/userProfile/v1/users"

# SEN account record for the signed-in user. Unlike the dms.api "devices/accounts/me" endpoint (which only
# carries account id + devices), this returns the private account profile -- including the email address --
# and is reachable with the ordinary mobile access token. Self-only: it always describes the authenticated
# account.
_ACCOUNT_ME_URL = "https://accounts.api.playstation.com/api/v1/accounts/me"
_MY_ACCOUNT_URL = "https://dms.api.playstation.com/api/v1/devices/accounts/me"


@dataclass(frozen=True, slots=True)
class Account:
    """Identifies a PlayStation Network account.

    :param account_id: The numeric PSN account id.
    :param online_id: The public PSN online id (username).
    :param region: The account's region (ISO country name), if resolvable.
    """

    account_id: str
    online_id: str
    region: str | None = None


def _primary_email_entry(account: Any) -> dict[str, Any] | None:
    """Select the primary email entry from an ``accounts/me`` response.

    Prefers the entry flagged ``isMain`` in ``emailAddresses``, then the first listed entry, then a
    synthetic entry built from the top-level ``signinId`` (the sign-in email) -- which carries no
    ``isVerified`` flag of its own, since Sony only reports that flag per ``emailAddresses`` entry. Returns
    ``None`` if no email is present at all.
    """
    if not isinstance(account, dict):
        return None
    emails = account.get("emailAddresses")
    if isinstance(emails, list):
        entries = [entry for entry in emails if isinstance(entry, dict) and entry.get("address")]
        chosen = next((entry for entry in entries if entry.get("isMain")), None) or next(iter(entries), None)
        if chosen is not None:
            return chosen
    signin = account.get("signinId")
    if isinstance(signin, str) and signin:
        return {"address": signin}
    return None


def _primary_email(account: Any) -> str | None:
    """Extract the primary email address from an ``accounts/me`` response.

    Prefers the address flagged ``isMain`` in ``emailAddresses``, then the first listed address, then the
    top-level ``signinId`` (the sign-in email). Returns ``None`` if no email is present.
    """
    entry = _primary_email_entry(account)
    return str(entry["address"]) if entry is not None else None


def _region_from_npid(npid: str) -> str | None:
    """Decode a legacy-profile ``npId`` to its region's country name (e.g. ``"US"`` -> ``"United States"``).

    The npId is a base64 string ending in ``.<ISO-3166-1-alpha-2 code>`` (e.g. ``"VaultTec-Co@b7.us"``);
    ``pycountry`` maps the code to a name.

    :param npid: The base64-encoded npId string.
    :returns: The country name, or ``None`` if the npId can't be decoded or carries no valid region code.
    """
    try:
        decoded = base64.b64decode(npid).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    if "@" not in decoded or "." not in decoded:
        return None
    code = decoded.rsplit(".", 1)[-1]
    if len(code) != 2 or not code.isalpha():
        return None
    country = pycountry.countries.get(alpha_2=code.upper())
    return country.name if country is not None else None


class AccountClient:
    """PSN account identity/email operations for the authenticated user.

    :param session: The authenticated :class:`~curator.psn.session.PsnSession` to call through.
    """

    def __init__(self, session: PsnSession) -> None:
        self._session = session

    async def whoami(self) -> Account:
        """Return the authenticated user's account id, online id, and region.

        :returns: The signed-in :class:`Account`.
        """
        return await self._session.run_with_reauth(self._whoami)

    async def _whoami(self) -> Account:
        account_id = await self._native_own_account_id()
        online_id = (await self._session.get(f"{_PROFILE_URI}/{account_id}/profiles")).json()["onlineId"]
        profile = (
            await self._session.get(f"{_LEGACY_PROFILE_URI}/{online_id}/profile2", params={"fields": "npId"})
        ).json()
        npid = (profile.get("profile") or {}).get("npId", "")
        return Account(account_id=account_id, online_id=online_id, region=_region_from_npid(npid))

    async def account_email(self) -> str | None:
        """Return the authenticated user's primary account email address.

        Reads the private SEN account record (``accounts.api.playstation.com``), which the ordinary mobile
        access token can reach. Returns the address flagged as the account's main email (equivalently, the
        sign-in id), or ``None`` if the account exposes no email. Self-only -- never another user's email.

        :returns: The primary email address, or ``None``.
        """
        return await self._session.run_with_reauth(self._account_email)

    async def _account_email(self) -> str | None:
        response = await self._session.get(_ACCOUNT_ME_URL)
        return _primary_email(response.json())

    async def account_email_verified(self) -> tuple[str, bool] | None:
        """Return the authenticated user's primary account email address plus its verified status.

        Reads the same private SEN account record as :meth:`account_email`
        (``accounts.api.playstation.com``), selecting the same entry (``isMain`` first, else the first
        listed address, else the ``signinId`` fallback), but also reports whether Sony has that address
        flagged ``isVerified``. The ``signinId`` fallback carries no ``isVerified`` flag of its own, so it
        is always reported unverified. This is the same sanctioned-narrow-surface as :meth:`account_email`:
        one address and one bool, transiently, never persisted, never the wider PII record.

        :returns: A ``(address, is_verified)`` tuple, or ``None`` if the account exposes no email.
        """
        return await self._session.run_with_reauth(self._account_email_verified)

    async def _account_email_verified(self) -> tuple[str, bool] | None:
        response = await self._session.get(_ACCOUNT_ME_URL)
        entry = _primary_email_entry(response.json())
        if entry is None:
            return None
        return str(entry["address"]), bool(entry.get("isVerified"))

    async def _native_own_account_id(self) -> str:
        """Resolve the authenticated account's id via the native session."""
        response = await self._session.get(_MY_ACCOUNT_URL)
        return str(response.json()["accountId"])
