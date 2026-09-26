"""Tests for AccountClient, using a hand-written fake session (no network, no credentials).

Ported from ``psnpy``'s ``test_client.py``, split out to the account-identity subset of that file's
assertions (the rest moved to ``test_psn_library_client.py``/``test_psn_catalog_client.py``).
"""

from __future__ import annotations

import base64
import random

import pycountry

from curator.psn._identity import (
    ACCOUNT_ID_KEY,
    MY_ACCOUNT_URL,
    ONLINE_ID_KEY,
    PROFILE_KEY,
    legacy_profile_url,
    profiles_url,
)
from curator.psn.account_client import (
    ACCOUNT_ME_URL,
    ADDRESS_KEY,
    EMAIL_ADDRESSES_KEY,
    IS_MAIN_KEY,
    IS_VERIFIED_KEY,
    NP_ID_KEY,
    SIGNIN_ID_KEY,
    Account,
    AccountClient,
    _primary_email,
)
from test_values import lowercase_token, new_account_id, new_email_address, new_online_id


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


def _npid(decoded: str) -> str:
    return base64.b64encode(decoded.encode()).decode()


def _npid_for_region(alpha_2: str) -> str:
    """An npId whose decoded form ends in ``.<alpha_2>``, the shape ``_region_from_npid`` reads."""
    return _npid(f"{lowercase_token()}@{lowercase_token(2)}.{alpha_2.lower()}")


class FakeSession:
    """Stands in for a ``curator.psn.session.PsnSession`` instance, answering by exact URL."""

    def __init__(self, *, account_id=None, online_id=None, npid=None, account_body=None):
        self._account_id = account_id or new_account_id()
        self._online_id = online_id or new_online_id()
        self._responses = {
            MY_ACCOUNT_URL: {ACCOUNT_ID_KEY: self._account_id},
            profiles_url(self._account_id): {ONLINE_ID_KEY: self._online_id},
            legacy_profile_url(self._online_id): {PROFILE_KEY: {NP_ID_KEY: npid or _npid(lowercase_token())}},
            ACCOUNT_ME_URL: account_body or {},
        }
        self.get_urls: list[str] = []

    async def get(self, url, params=None, headers=None):
        self.get_urls.append(url)
        return FakeResponse(self._responses[url])

    async def run_with_reauth(self, operation):
        return await operation()


def _entry(address, **flags):
    return {ADDRESS_KEY: address, **flags}


async def test_whoami_maps_fields():
    account_id, online_id = new_account_id(), new_online_id()
    country = random.choice(list(pycountry.countries))
    session = FakeSession(account_id=account_id, online_id=online_id, npid=_npid_for_region(country.alpha_2))

    account = await AccountClient(session).whoami()

    assert account == Account(account_id=account_id, online_id=online_id, region=country.name)


async def test_whoami_region_none_when_unresolved():
    client = AccountClient(FakeSession(npid=_npid(lowercase_token())))

    assert (await client.whoami()).region is None


def test_primary_email_prefers_main_address():
    main_address = new_email_address()
    account = {
        EMAIL_ADDRESSES_KEY: [
            _entry(new_email_address(), **{IS_MAIN_KEY: False}),
            _entry(main_address, **{IS_MAIN_KEY: True}),
        ],
        SIGNIN_ID_KEY: new_email_address(),
    }

    assert _primary_email(account) == main_address


def test_primary_email_falls_back_to_the_first_listed_address():
    only_address = new_email_address()

    assert _primary_email({EMAIL_ADDRESSES_KEY: [_entry(only_address)]}) == only_address


def test_primary_email_falls_back_to_the_signin_id():
    signin = new_email_address()

    assert _primary_email({EMAIL_ADDRESSES_KEY: [], SIGNIN_ID_KEY: signin}) == signin


def test_primary_email_none_when_absent():
    assert _primary_email({}) is None
    assert _primary_email({EMAIL_ADDRESSES_KEY: [{IS_MAIN_KEY: True}]}) is None
    assert _primary_email(None) is None


async def test_account_email_reads_accounts_me():
    address = new_email_address()
    body = {EMAIL_ADDRESSES_KEY: [_entry(address, **{IS_MAIN_KEY: True})], SIGNIN_ID_KEY: address}
    session = FakeSession(account_body=body)

    assert await AccountClient(session).account_email() == address
    assert session.get_urls == [ACCOUNT_ME_URL]


async def test_account_email_verified_true_when_main_entry_verified():
    address = new_email_address()
    body = {EMAIL_ADDRESSES_KEY: [_entry(address, **{IS_MAIN_KEY: True, IS_VERIFIED_KEY: True})]}

    assert await AccountClient(FakeSession(account_body=body)).account_email_verified() == (address, True)


async def test_account_email_verified_false_when_explicitly_false():
    address = new_email_address()
    body = {EMAIL_ADDRESSES_KEY: [_entry(address, **{IS_MAIN_KEY: True, IS_VERIFIED_KEY: False})]}

    assert await AccountClient(FakeSession(account_body=body)).account_email_verified() == (address, False)


async def test_account_email_verified_false_when_the_flag_is_missing():
    address = new_email_address()
    body = {EMAIL_ADDRESSES_KEY: [_entry(address, **{IS_MAIN_KEY: True})]}

    assert await AccountClient(FakeSession(account_body=body)).account_email_verified() == (address, False)


async def test_account_email_verified_uses_main_entrys_own_flag():
    main_address = new_email_address()
    body = {
        EMAIL_ADDRESSES_KEY: [
            _entry(new_email_address(), **{IS_MAIN_KEY: False, IS_VERIFIED_KEY: True}),
            _entry(main_address, **{IS_MAIN_KEY: True, IS_VERIFIED_KEY: False}),
        ],
    }

    assert await AccountClient(FakeSession(account_body=body)).account_email_verified() == (main_address, False)


async def test_account_email_verified_signin_fallback_is_unverified():
    signin = new_email_address()
    body = {EMAIL_ADDRESSES_KEY: [], SIGNIN_ID_KEY: signin}

    assert await AccountClient(FakeSession(account_body=body)).account_email_verified() == (signin, False)


async def test_account_email_verified_none_when_absent():
    assert await AccountClient(FakeSession(account_body={})).account_email_verified() is None
