"""Tests for PresenceClient, using a hand-written fake session (no network, no credentials)."""

from __future__ import annotations

from curator.psn.models import Presence
from curator.psn.presence_client import PresenceClient


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *, own_account_id="123", basic_presence=None, batch_body=None):
        self._own_account_id = own_account_id
        self._basic_presence = basic_presence or {}
        self._batch_body = batch_body or {}
        self.get_calls: list[str] = []

    async def get(self, url, params=None, headers=None):
        self.get_calls.append(url)
        if "devices/accounts/me" in url:
            return FakeResponse({"accountId": self._own_account_id})
        if url.endswith("/basicPresences") and "accountIds" in (params or {}):
            return FakeResponse(self._batch_body)
        if "basicPresences" in url:
            return FakeResponse({"basicPresence": self._basic_presence})
        return FakeResponse({})

    async def run_with_reauth(self, operation):
        return await operation()


async def test_presence_for_target_account_id():
    basic = {
        "availability": "availableToPlay",
        "primaryPlatformInfo": {"platform": "PS5", "lastOnlineDate": "2024-01-01T00:00:00Z"},
        "gameTitleInfoList": [{"titleName": "Bloodborne"}],
    }
    client = PresenceClient(FakeSession(basic_presence=basic))

    presence = await client.presence(account_id="999")

    assert presence == Presence(
        online_status="availableToPlay",
        platform="PS5",
        last_online_date="2024-01-01T00:00:00Z",
        game_title="Bloodborne",
    )


async def test_presence_defaults_to_authenticated_user():
    session = FakeSession(own_account_id="555", basic_presence={})
    client = PresenceClient(session)

    await client.presence()

    assert any("555" in url for url in session.get_calls)


async def test_presence_batch_keys_by_account_id():
    batch_body = {
        "basicPresences": [
            {"accountId": "1", "availability": "online"},
            {"accountId": "2", "availability": "offline"},
        ]
    }
    client = PresenceClient(FakeSession(batch_body=batch_body))

    result = await client.presence_batch(["1", "2"])

    assert set(result.keys()) == {"1", "2"}
    assert result["1"].online_status == "online"
    assert result["2"].online_status == "offline"


async def test_presence_batch_empty_when_no_entries():
    client = PresenceClient(FakeSession(batch_body={}))

    assert await client.presence_batch(["1"]) == {}
