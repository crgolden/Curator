"""Shared account-id/online-id resolution helpers.

``curator.psn.trophy_client``, ``presence_client``, and ``social_client`` all let a caller target another
PSN user by either their ``online_id`` or their ``account_id`` (or omit both for "the authenticated
user") -- this factors that resolution logic out once instead of duplicating it per client.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from curator.psn.session import PsnSession

_PROFILE_URI = "https://m.np.playstation.com/api/userProfile/v1/internal/users"
_LEGACY_PROFILE_URI = "https://us-prof.np.community.playstation.net/userProfile/v1/users"
_MY_ACCOUNT_URL = "https://dms.api.playstation.com/api/v1/devices/accounts/me"


async def own_account_id(session: PsnSession) -> str:
    """Resolve the authenticated account's id via the native session."""
    response = await session.get(_MY_ACCOUNT_URL)
    return str(response.json()["accountId"])


async def account_id_for(session: PsnSession, online_id: str | None, account_id: str | None) -> str:
    """Resolve a target's account id; ``None``/``None`` means the authenticated user."""
    if account_id is not None:
        return account_id
    if online_id is not None:
        data = (
            await session.get(f"{_LEGACY_PROFILE_URI}/{online_id}/profile2", params={"fields": "accountId"})
        ).json()["profile"]
        return str(data["accountId"])
    return await own_account_id(session)


async def online_id_for(session: PsnSession, account_id: str) -> str | None:
    """Resolve the online id for an account id (used mapping ``SocialUser`` results)."""
    data = (await session.get(f"{_PROFILE_URI}/{account_id}/profiles")).json()
    online_id = data.get("onlineId")
    return str(online_id) if online_id is not None else None


async def target_online_id(session: PsnSession, online_id: str | None, account_id: str | None) -> str:
    """Resolve a target's online id; ``None``/``None`` means the authenticated user."""
    if online_id is not None:
        return online_id
    if account_id is not None:
        resolved = await online_id_for(session, account_id)
    else:
        resolved = await online_id_for(session, await own_account_id(session))
    return resolved or ""


async def path_account_id(session: PsnSession, online_id: str | None, account_id: str | None) -> str:
    """Return the ``{account_id}`` path segment for a target: literal ``"me"`` for self, else resolved."""
    if online_id is None and account_id is None:
        return "me"
    return await account_id_for(session, online_id, account_id)
