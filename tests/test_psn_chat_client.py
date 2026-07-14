"""Tests for ChatClient, using a hand-written fake session (no network, no credentials)."""

from __future__ import annotations

from curator.psn.chat_client import ChatClient
from curator.psn.models import ChatGroup, ChatMessage, SocialUser


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *, group_list_body=None, group_info_bodies=None, conversation_body=None):
        self._group_list_body = group_list_body or {}
        self._group_info_bodies = dict(group_info_bodies or {})
        self._conversation_body = conversation_body or {}
        self.get_calls: list[str] = []

    async def get(self, url, params=None, headers=None):
        self.get_calls.append(url)
        if "/threads/" in url:
            return FakeResponse(self._conversation_body)
        if url.endswith("/members/me/groups"):
            return FakeResponse(self._group_list_body)
        for group_id, body in self._group_info_bodies.items():
            if url.endswith(f"/groups/{group_id}"):
                return FakeResponse(body)
        return FakeResponse({})

    async def run_with_reauth(self, operation):
        return await operation()


async def test_chat_groups_fetches_each_groups_info():
    list_body = {"groups": [{"groupId": "g1"}, {"groupId": "g2"}]}
    info_bodies = {
        "g1": {"groupId": "g1", "groupName": {"value": "Squad"}, "members": [{"accountId": "1", "onlineId": "A"}]},
        "g2": {"groupId": "g2", "members": []},
    }
    client = ChatClient(FakeSession(group_list_body=list_body, group_info_bodies=info_bodies))

    groups = await client.chat_groups()

    assert groups == [
        ChatGroup(
            group_id="g1",
            name="Squad",
            favorite=None,
            member_count=1,
            members=(SocialUser(account_id="1", online_id="A"),),
            modified_at=None,
        ),
        ChatGroup(group_id="g2", name=None, favorite=None, member_count=0, members=(), modified_at=None),
    ]


async def test_group_info_derives_name_from_blank_group_name():
    info_bodies = {"g1": {"groupId": "g1", "groupName": {"value": "  "}, "members": []}}
    client = ChatClient(FakeSession(group_info_bodies=info_bodies))

    group = await client.group_info("g1")

    assert group.name is None


async def test_conversation_maps_messages():
    body = {
        "messages": [
            {
                "messageUid": "m1",
                "body": "hello",
                "messageType": 1,
                "createdTimestamp": 1704067200000,
                "sender": {"accountId": "1", "onlineId": "Alice"},
            }
        ]
    }
    client = ChatClient(FakeSession(conversation_body=body))

    messages = await client.conversation("g1")

    assert messages == [
        ChatMessage(
            message_uid="m1",
            body="hello",
            message_type=1,
            created_at="2024-01-01T00:00:00+00:00",
            sender=SocialUser(account_id="1", online_id="Alice"),
        )
    ]


async def test_conversation_empty_when_no_messages():
    client = ChatClient(FakeSession(conversation_body={}))

    assert await client.conversation("g1") == []
