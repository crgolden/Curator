"""Tests for MutationService, using hand-written fake session/repository (no network, no credentials).

Ported from ``psnpy``'s ``test_mutations.py``, split to the actual-mutation subset.
"""

from __future__ import annotations

import pytest

from curator.psn.errors import MutationNotAllowedError
from curator.psn.mutation_service import MutationService
from curator.psn.safety import MutationGuard


class FakeResponse:
    def __init__(self, body=None):
        self._body = body or {}

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *, own_account_id="pinned-acct"):
        self._own_account_id = own_account_id
        self.post_calls: list[tuple[str, dict]] = []
        self.patch_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[str] = []
        self.put_calls: list[str] = []
        self._post_response: dict = {}

    async def get(self, url, params=None, headers=None):
        if "devices/accounts/me" in url:
            return FakeResponse({"accountId": self._own_account_id})
        if url.endswith("/profiles"):
            return FakeResponse({"onlineId": "someone"})
        if "/profile2" in url:
            return FakeResponse({"profile": {"accountId": "resolved-acct"}})
        return FakeResponse({})

    async def post(self, url, json=None, data=None, params=None, headers=None):
        self.post_calls.append((url, json or {}))
        return FakeResponse(self._post_response)

    async def patch(self, url, json=None, headers=None):
        self.patch_calls.append((url, json or {}))
        return FakeResponse({})

    async def put(self, url, headers=None):
        self.put_calls.append(url)
        return FakeResponse({})

    async def delete(self, url, headers=None):
        self.delete_calls.append(url)
        return FakeResponse({})

    async def run_with_reauth(self, operation):
        return await operation()


class FakePinnedAccountRepository:
    def __init__(self, pinned_account_id=None):
        self.pinned = pinned_account_id

    async def get_pinned_account_id(self, identity_sub):
        return self.pinned

    async def pin(self, identity_sub, psn_account_id):
        self.pinned = psn_account_id


class FakeLink:
    def __init__(self, psn_account_id, *, allow_friend_writes=True, allow_chat_writes=True):
        self.psn_account_id = psn_account_id
        self.allow_friend_writes = allow_friend_writes
        self.allow_chat_writes = allow_chat_writes


class FakeLinkReader:
    def __init__(self, link):
        self.link = link

    async def get_link(self, sub):
        return self.link


def _service(session, *, linked="pinned-acct", allow_friend_writes=True, allow_chat_writes=True):
    link = (
        None
        if linked is None
        else FakeLink(linked, allow_friend_writes=allow_friend_writes, allow_chat_writes=allow_chat_writes)
    )
    guard = MutationGuard("sub-1", FakePinnedAccountRepository(), links=FakeLinkReader(link))
    return MutationService(session, guard)


async def test_create_group_rejected_when_not_the_linked_account():
    session = FakeSession(own_account_id="some-other-account")
    service = _service(session, linked="pinned-acct")

    with pytest.raises(MutationNotAllowedError):
        await service.create_group(account_ids=["999"])

    assert session.post_calls == []


async def test_create_group_rejected_when_chat_writes_not_consented():
    session = FakeSession(own_account_id="pinned-acct")
    service = _service(session, allow_chat_writes=False)

    with pytest.raises(MutationNotAllowedError, match="allow_chat_writes"):
        await service.create_group(account_ids=["999"])

    assert session.post_calls == []


async def test_friend_mutation_rejected_when_only_chat_writes_consented():
    session = FakeSession(own_account_id="pinned-acct")
    service = _service(session, allow_friend_writes=False, allow_chat_writes=True)

    with pytest.raises(MutationNotAllowedError, match="allow_friend_writes"):
        await service.accept_friend(account_id="999")

    assert session.put_calls == []


async def test_mutation_rejected_when_no_psn_account_is_linked():
    session = FakeSession(own_account_id="pinned-acct")
    service = _service(session, linked=None)

    with pytest.raises(MutationNotAllowedError, match="No PSN account is linked"):
        await service.create_group(account_ids=["999"])

    assert session.post_calls == []


async def test_create_group_succeeds_for_linked_and_consented_account():
    session = FakeSession(own_account_id="pinned-acct")
    session._post_response = {"groupId": "new-group"}
    service = _service(session)

    group_id = await service.create_group(account_ids=["999"])

    assert group_id == "new-group"
    assert session.post_calls[0][1] == {"invitees": [{"accountId": "999"}]}


async def test_rename_group_sends_patch():
    session = FakeSession()
    service = _service(session)

    await service.rename_group("g1", "New Name")

    assert session.patch_calls == [
        ("https://m.np.playstation.com/api/gamingLoungeGroups/v1/groups/g1", {"groupName": {"value": "New Name"}})
    ]


async def test_send_message_maps_response():
    session = FakeSession()
    session._post_response = {"messageUid": "m1", "createdTimestamp": 1704067200000}
    service = _service(session)

    sent = await service.send_message("g1", "hi there")

    assert sent.message_uid == "m1"
    assert sent.created_at == "2024-01-01T00:00:00+00:00"


async def test_invite_to_group_resolves_online_ids_to_account_ids():
    session = FakeSession()
    service = _service(session)

    await service.invite_to_group("g1", online_ids=["SomeOnlineId"])

    invitee_account_ids = [i["accountId"] for i in session.post_calls[0][1]["invitees"]]
    assert invitee_account_ids


async def test_invite_to_group_posts_to_the_named_group_not_the_create_group_endpoint():
    session = FakeSession()
    service = _service(session)

    await service.invite_to_group("g1", account_ids=["999"])

    assert session.post_calls[0][0].endswith("/groups/g1/invitees")


async def test_create_group_posts_to_the_collection_so_psn_allocates_a_new_group():
    session = FakeSession()
    service = _service(session)

    await service.create_group(account_ids=["999"])

    assert session.post_calls[0][0].endswith("/groups")


async def test_kick_from_group_sends_delete():
    session = FakeSession()
    service = _service(session)

    await service.kick_from_group("g1", account_id="999")

    assert session.delete_calls == ["https://m.np.playstation.com/api/gamingLoungeGroups/v1/groups/g1/members/999"]


async def test_leave_group_sends_delete_for_me():
    session = FakeSession()
    service = _service(session)

    await service.leave_group("g1")

    assert session.delete_calls == ["https://m.np.playstation.com/api/gamingLoungeGroups/v1/groups/g1/members/me"]


async def test_accept_friend_sends_put():
    session = FakeSession()
    service = _service(session)

    await service.accept_friend(account_id="999")

    assert session.put_calls == ["https://m.np.playstation.com/api/userProfile/v1/internal/users/me/friends/999"]


async def test_remove_friend_sends_delete():
    session = FakeSession()
    service = _service(session)

    await service.remove_friend(account_id="999")

    assert session.delete_calls == ["https://m.np.playstation.com/api/userProfile/v1/internal/users/me/friends/999"]


async def test_every_mutation_checks_the_live_account_before_acting():
    session = FakeSession(own_account_id="wrong-account")
    service = _service(session, linked="pinned-acct")

    for coro in (
        service.rename_group("g1", "x"),
        service.send_message("g1", "x"),
        service.kick_from_group("g1", account_id="1"),
        service.leave_group("g1"),
        service.accept_friend(account_id="1"),
        service.remove_friend(account_id="1"),
    ):
        with pytest.raises(MutationNotAllowedError):
            await coro

    assert session.post_calls == []
    assert session.patch_calls == []
    assert session.delete_calls == []
    assert session.put_calls == []
