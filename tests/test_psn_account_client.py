"""Tests for AccountClient, using a hand-written fake session (no network, no credentials).

Ported from ``psnpy``'s ``test_client.py``, split out to the account-identity subset of that file's
assertions (the rest moved to ``test_psn_library_client.py``/``test_psn_catalog_client.py``).
"""

from __future__ import annotations

import base64

from curator.psn.account_client import Account, AccountClient, _primary_email


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _npid_for_region(region: str | None) -> str:
    """Build a base64 npId that ``_region_from_npid`` decodes back to ``region`` (an ISO alpha-2 code)."""
    raw = f"VaultTec-Co@b7.{region.lower()}" if region else "no-region-here"
    return base64.b64encode(raw.encode()).decode()


class FakeSession:
    """Stands in for a ``curator.psn.session.PsnSession`` instance."""

    def __init__(self, account_id="123", online_id="Tester", region_code="US", account_body=None):
        self._account_id = account_id
        self._online_id = online_id
        self._region_code = region_code
        self._account_body = account_body
        self.get_urls: list[str] = []

    async def get(self, url, params=None, headers=None):
        self.get_urls.append(url)
        if "devices/accounts/me" in url:
            return FakeResponse({"accountId": self._account_id})
        if url.endswith("/profiles"):
            return FakeResponse({"onlineId": self._online_id})
        if "/profile2" in url:
            return FakeResponse({"profile": {"npId": _npid_for_region(self._region_code)}})
        return FakeResponse(self._account_body or {})

    async def run_with_reauth(self, operation):
        return await operation()


async def test_whoami_maps_fields():
    session = FakeSession(account_id="999", online_id="VaultTec", region_code="US")
    client = AccountClient(session)

    account = await client.whoami()

    assert account == Account(account_id="999", online_id="VaultTec", region="United States")


async def test_whoami_region_none_when_unresolved():
    session = FakeSession(region_code=None)
    client = AccountClient(session)

    assert (await client.whoami()).region is None


def test_primary_email_prefers_main_address():
    account = {
        "emailAddresses": [
            {"address": "old@example.com", "isMain": False},
            {"address": "main@example.com", "isMain": True},
        ],
        "signinId": "signin@example.com",
    }

    assert _primary_email(account) == "main@example.com"


def test_primary_email_falls_back_to_first_then_signin():
    no_main = {"emailAddresses": [{"address": "only@example.com"}]}
    assert _primary_email(no_main) == "only@example.com"

    signin_only = {"emailAddresses": [], "signinId": "signin@example.com"}
    assert _primary_email(signin_only) == "signin@example.com"


def test_primary_email_none_when_absent():
    assert _primary_email({}) is None
    assert _primary_email({"emailAddresses": [{"isMain": True}]}) is None
    assert _primary_email(None) is None


async def test_account_email_reads_accounts_me():
    body = {"emailAddresses": [{"address": "chris@example.com", "isMain": True}], "signinId": "chris@example.com"}
    session = FakeSession(account_body=body)
    client = AccountClient(session)

    assert await client.account_email() == "chris@example.com"
    assert session.get_urls == ["https://accounts.api.playstation.com/api/v1/accounts/me"]


async def test_account_email_verified_true_when_main_entry_verified():
    body = {"emailAddresses": [{"address": "chris@example.com", "isMain": True, "isVerified": True}]}
    client = AccountClient(FakeSession(account_body=body))

    assert await client.account_email_verified() == ("chris@example.com", True)


async def test_account_email_verified_false_when_explicit_false_or_missing():
    explicit_false = {"emailAddresses": [{"address": "chris@example.com", "isMain": True, "isVerified": False}]}
    client = AccountClient(FakeSession(account_body=explicit_false))
    assert await client.account_email_verified() == ("chris@example.com", False)

    missing_flag = {"emailAddresses": [{"address": "chris@example.com", "isMain": True}]}
    client = AccountClient(FakeSession(account_body=missing_flag))
    assert await client.account_email_verified() == ("chris@example.com", False)


async def test_account_email_verified_uses_main_entrys_own_flag():
    # Two entries: the non-main one is verified, the isMain one is not. The result must reflect the CHOSEN
    # (isMain) entry's flag, not any-verified-entry-wins.
    body = {
        "emailAddresses": [
            {"address": "old@example.com", "isMain": False, "isVerified": True},
            {"address": "main@example.com", "isMain": True, "isVerified": False},
        ],
    }
    client = AccountClient(FakeSession(account_body=body))

    assert await client.account_email_verified() == ("main@example.com", False)


async def test_account_email_verified_signin_fallback_is_unverified():
    body = {"emailAddresses": [], "signinId": "signin@example.com"}
    client = AccountClient(FakeSession(account_body=body))

    assert await client.account_email_verified() == ("signin@example.com", False)


async def test_account_email_verified_none_when_absent():
    client = AccountClient(FakeSession(account_body={}))

    assert await client.account_email_verified() is None
