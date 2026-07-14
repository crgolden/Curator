"""Tests for SocialClient, using a hand-written fake session (no network, no credentials).

Ported from ``psnpy``'s ``test_social.py``/``test_capabilities.py``.
"""

from __future__ import annotations

import pytest

from curator.psn.models import AccountDevice, Friendship, PlayerSearchResult, Profile, ProfileShareLink, SocialUser
from curator.psn.social_client import SocialClient


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *, own_account_id="123", responses=None, online_ids=None, devices_body=None):
        self._own_account_id = own_account_id
        self._responses = dict(responses or {})
        self._online_ids = dict(online_ids or {})
        self._devices_body = devices_body
        self.get_calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, headers=None):
        self.get_calls.append((url, params or {}))
        if "devices/accounts/me" in url:
            if self._devices_body is not None and "includeFields" in (params or {}):
                return FakeResponse(self._devices_body)
            return FakeResponse({"accountId": self._own_account_id})
        if url.endswith("/profiles"):
            account_id = url.rsplit("/", 2)[-2]
            return FakeResponse({"onlineId": self._online_ids.get(account_id, f"user-{account_id}")})
        operation_name = (params or {}).get("operationName")
        if operation_name:
            return FakeResponse(self._responses.get(operation_name, {}))
        for key, body in self._responses.items():
            if key in url:
                return FakeResponse(body)
        return FakeResponse({})

    async def run_with_reauth(self, operation):
        return await operation()


async def test_friends_resolves_online_ids():
    client = SocialClient(
        FakeSession(responses={"friends": {"friends": ["1", "2"]}}, online_ids={"1": "Alice", "2": "Bob"})
    )

    friends = await client.friends()

    assert friends == [SocialUser(account_id="1", online_id="Alice"), SocialUser(account_id="2", online_id="Bob")]


async def test_blocked_resolves_online_ids():
    client = SocialClient(FakeSession(responses={"me/blocks": {"blockList": ["9"]}}, online_ids={"9": "Baddie"}))

    blocked = await client.blocked()

    assert blocked == [SocialUser(account_id="9", online_id="Baddie")]


async def test_available_to_play_resolves_online_ids():
    body = {"settings": [{"accountId": "5"}]}
    client = SocialClient(FakeSession(responses={"availableToPlay": body}, online_ids={"5": "Casey"}))

    result = await client.available_to_play()

    assert result == [SocialUser(account_id="5", online_id="Casey")]


async def test_friend_requests_resolves_online_ids():
    body = {"receivedRequests": [{"accountId": "7"}]}
    client = SocialClient(FakeSession(responses={"receivedRequests": body}, online_ids={"7": "Dana"}))

    result = await client.friend_requests()

    assert result == [SocialUser(account_id="7", online_id="Dana")]


async def test_friendship_maps_fields():
    body = {"friendRelation": "friend", "friendsCount": 10, "mutualFriendsCount": 3}
    client = SocialClient(FakeSession(responses={"summary": body}))

    result = await client.friendship(account_id="42")

    assert result == Friendship(
        relation="friend", personal_detail_sharing=None, friends_count=10, mutual_friends_count=3
    )


async def test_friendship_requires_a_target():
    client = SocialClient(FakeSession())

    with pytest.raises(ValueError, match="requires a target"):
        await client.friendship()


async def test_profile_never_hydrates_personal_detail():
    body = {"profile": {"aboutMe": "Hello", "avatarUrls": [{"avatarUrl": "a.png"}], "isOfficiallyVerified": True}}
    client = SocialClient(FakeSession(responses={"profile2": body}, online_ids={"123": "Me"}))

    profile = await client.profile()

    assert profile == Profile(
        about_me="Hello",
        avatars=("a.png",),
        languages=(),
        is_officially_verified=True,
        personal_detail=None,
    )


async def test_is_blocked_true_and_false():
    client = SocialClient(FakeSession(responses={"me/blocks": {"blockList": ["7"]}}))

    assert await client.is_blocked(account_id="7") is True
    assert await client.is_blocked(account_id="8") is False


async def test_is_blocked_requires_a_target():
    client = SocialClient(FakeSession())

    with pytest.raises(ValueError, match="requires a target"):
        await client.is_blocked()


async def test_devices_maps_fields():
    body = {
        "accountDevices": [
            {
                "deviceId": "d1",
                "deviceType": "PS5",
                "deviceName": "My PS5",
                "activationType": "PRIMARY",
                "activationDate": "2020-01-01",
            }
        ]
    }
    client = SocialClient(FakeSession(devices_body=body))

    devices = await client.devices()

    assert devices == [
        AccountDevice(
            device_id="d1",
            device_type="PS5",
            device_name="My PS5",
            activation_type="PRIMARY",
            activation_date="2020-01-01",
            deactivation_date=None,
        )
    ]


async def test_share_link_maps_fields():
    body = {"shareUrl": "https://psn/share", "shareImageUrl": "qr.png", "shareImageUrlDestination": "https://psn/dest"}
    client = SocialClient(FakeSession(responses={"share/profile": body}))

    result = await client.share_link()

    assert result == ProfileShareLink(
        share_url="https://psn/share",
        share_image_url="qr.png",
        share_image_url_destination="https://psn/dest",
    )


async def test_search_players_maps_first_page():
    context_response = {
        "data": {
            "universalContextSearch": {
                "results": [
                    {
                        "searchResults": [
                            {"result": {"accountId": "1", "onlineId": "Alice", "isPsPlus": True}},
                        ],
                        "next": "",
                    }
                ]
            }
        }
    }
    client = SocialClient(FakeSession(responses={"metGetContextSearchResults": context_response}))

    results = await client.search_players("alice")

    assert results == [
        PlayerSearchResult(account_id="1", online_id="Alice", avatar_url=None, is_ps_plus=True, relationship=None)
    ]
