"""Tests for PresenceClient, using a hand-written fake session (no network, no credentials)."""

from __future__ import annotations

from curator.psn._identity import ACCOUNT_ID_KEY, MY_ACCOUNT_URL
from curator.psn.models import Presence
from curator.psn.presence_client import (
    AVAILABILITY_KEY,
    BASIC_PRESENCE_KEY,
    BASIC_PRESENCES_KEY,
    GAME_TITLE_INFO_LIST_KEY,
    LAST_ONLINE_DATE_KEY,
    PLATFORM_KEY,
    PRIMARY_PLATFORM_INFO_KEY,
    TITLE_NAME_KEY,
    PresenceClient,
    basic_presences_url,
    batch_basic_presences_url,
)
from curator.psn.title_platform import CONSOLE_PLATFORM_IDS
from test_values import lowercase_token, new_account_id, new_game_title, new_utc_instant


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    """Answers by exact URL; ``responses`` adds URLs beyond the caller's own account lookup."""

    def __init__(self, *, own_account_id=None, responses=None):
        self._responses = {MY_ACCOUNT_URL: {ACCOUNT_ID_KEY: own_account_id or new_account_id()}, **(responses or {})}
        self.get_calls: list[str] = []

    async def get(self, url, params=None, headers=None):
        self.get_calls.append(url)
        return FakeResponse(self._responses[url])

    async def run_with_reauth(self, operation):
        return await operation()


async def test_presence_for_target_account_id():
    account_id = new_account_id()
    expected = Presence(
        online_status=lowercase_token(),
        platform=CONSOLE_PLATFORM_IDS[0],
        last_online_date=new_utc_instant().isoformat(),
        game_title=new_game_title(),
    )
    basic = {
        AVAILABILITY_KEY: expected.online_status,
        PRIMARY_PLATFORM_INFO_KEY: {PLATFORM_KEY: expected.platform, LAST_ONLINE_DATE_KEY: expected.last_online_date},
        GAME_TITLE_INFO_LIST_KEY: [{TITLE_NAME_KEY: expected.game_title}],
    }
    client = PresenceClient(FakeSession(responses={basic_presences_url(account_id): {BASIC_PRESENCE_KEY: basic}}))

    presence = await client.presence(account_id=account_id)

    assert presence == expected


async def test_presence_defaults_to_authenticated_user():
    own_account_id = new_account_id()
    session = FakeSession(
        own_account_id=own_account_id, responses={basic_presences_url(own_account_id): {BASIC_PRESENCE_KEY: {}}}
    )

    await PresenceClient(session).presence()

    assert session.get_calls == [MY_ACCOUNT_URL, basic_presences_url(own_account_id)]


async def test_presence_batch_keys_by_account_id():
    first_account_id, second_account_id = new_account_id(), new_account_id()
    first_status, second_status = lowercase_token(), lowercase_token()
    batch_body = {
        BASIC_PRESENCES_KEY: [
            {ACCOUNT_ID_KEY: first_account_id, AVAILABILITY_KEY: first_status},
            {ACCOUNT_ID_KEY: second_account_id, AVAILABILITY_KEY: second_status},
        ]
    }
    client = PresenceClient(FakeSession(responses={batch_basic_presences_url(): batch_body}))

    result = await client.presence_batch([first_account_id, second_account_id])

    assert result == {
        first_account_id: Presence(online_status=first_status),
        second_account_id: Presence(online_status=second_status),
    }


async def test_presence_batch_empty_when_no_entries():
    client = PresenceClient(FakeSession(responses={batch_basic_presences_url(): {}}))

    assert await client.presence_batch([new_account_id()]) == {}
