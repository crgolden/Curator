"""Async client for PSN account identity and email -- the account-facing slice ``curator.link_service``
uses.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Any, Final

import pycountry

from curator.psn._identity import (
    ACCOUNT_ID_KEY,
    FIELDS_PARAM,
    MY_ACCOUNT_URL,
    ONLINE_ID_KEY,
    PROFILE_KEY,
    legacy_profile_url,
    profiles_url,
)
from curator.psn.session import PsnSession

ACCOUNT_ME_URL: Final = "https://accounts.api.playstation.com/api/v1/accounts/me"

EMAIL_ADDRESSES_KEY: Final = "emailAddresses"
ADDRESS_KEY: Final = "address"
IS_MAIN_KEY: Final = "isMain"
IS_VERIFIED_KEY: Final = "isVerified"
SIGNIN_ID_KEY: Final = "signinId"
NP_ID_KEY: Final = "npId"


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
    emails = account.get(EMAIL_ADDRESSES_KEY)
    if isinstance(emails, list):
        entries = [entry for entry in emails if isinstance(entry, dict) and entry.get(ADDRESS_KEY)]
        chosen = next((entry for entry in entries if entry.get(IS_MAIN_KEY)), None) or next(iter(entries), None)
        if chosen is not None:
            return chosen
    signin = account.get(SIGNIN_ID_KEY)
    if isinstance(signin, str) and signin:
        return {ADDRESS_KEY: signin}
    return None


def _primary_email(account: Any) -> str | None:
    """Extract the primary email address from an ``accounts/me`` response.

    Prefers the address flagged ``isMain`` in ``emailAddresses``, then the first listed address, then the
    top-level ``signinId`` (the sign-in email). Returns ``None`` if no email is present.
    """
    entry = _primary_email_entry(account)
    return str(entry[ADDRESS_KEY]) if entry is not None else None


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
        online_id = (await self._session.get(profiles_url(account_id))).json()[ONLINE_ID_KEY]
        profile = (await self._session.get(legacy_profile_url(online_id), params={FIELDS_PARAM: NP_ID_KEY})).json()
        npid = (profile.get(PROFILE_KEY) or {}).get(NP_ID_KEY, "")
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
        response = await self._session.get(ACCOUNT_ME_URL)
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
        response = await self._session.get(ACCOUNT_ME_URL)
        entry = _primary_email_entry(response.json())
        if entry is None:
            return None
        return str(entry[ADDRESS_KEY]), bool(entry.get(IS_VERIFIED_KEY))

    async def _native_own_account_id(self) -> str:
        """Resolve the authenticated account's id via the native session."""
        response = await self._session.get(MY_ACCOUNT_URL)
        return str(response.json()[ACCOUNT_ID_KEY])


AccountClientFactory = Callable[[str], Coroutine[Any, Any, "AccountClient"]]
"""Builds a raw :class:`AccountClient` (never cached) for a given Identity ``sub``, used by
``curator.identity_routes`` for its one ``whoami()`` call. Requires an existing PSN link. Lives alongside
:class:`AccountClient` (rather than in ``curator.app``, where it's built) so both ``curator.app`` and
``curator.identity_routes`` can import it without the two importing each other -- mirrors
``curator.psn.trophy_client.TrophyClientFactory``."""
