"""Tests for MutationService, using hand-written fake session/repository (no network, no credentials).

Ported from ``psnpy``'s ``test_mutations.py``, split to the actual-mutation subset.
"""

from __future__ import annotations

import pytest

from curator.psn._identity import (
    ACCOUNT_ID_KEY,
    MY_ACCOUNT_URL,
    ONLINE_ID_KEY,
    PROFILE_KEY,
    SELF_PATH_ID,
    legacy_profile_url,
    profiles_url,
)
from curator.psn.errors import MutationNotAllowedError, NoPendingFriendRequestError
from curator.psn.mutation_service import (
    CREATED_TIMESTAMP_KEY,
    GROUP_NAME_KEY,
    INVITEES_KEY,
    MESSAGE_UID_KEY,
    NO_FRIEND_RELATION,
    VALUE_KEY,
    MutationService,
    group_invitees_url,
    group_member_url,
    group_url,
    groups_url,
)
from curator.psn.safety import CHAT_WRITES, FRIEND_WRITES, MutationGuard
from curator.psn.social_client import (
    FRIEND_RELATION_KEY,
    GROUP_ID_KEY,
    GROUPS_KEY,
    RECEIVED_REQUESTS_KEY,
    friend_url,
    friendship_summary_url,
    my_chat_groups_url,
    received_requests_url,
)
from test_values import (
    lowercase_token,
    new_account_id,
    new_game_title,
    new_group_id,
    new_identity_sub,
    new_online_id,
    new_opaque_token,
    new_utc_instant,
)


class FakeResponse:
    def __init__(self, body=None):
        self._body = body or {}

    def json(self):
        return self._body


class FakeSession:
    """Answers by exact URL. The caller's own account resolves through ``whoami``'s three reads, and every
    online id in ``account_ids_by_online_id`` resolves to its account id through the legacy profile."""

    def __init__(self, *, own_account_id=None, account_ids_by_online_id=None, routes=None):
        self.own_account_id = own_account_id or new_account_id()
        own_online_id = new_online_id()
        account_ids = {own_online_id: self.own_account_id, **(account_ids_by_online_id or {})}
        legacy_profiles = {
            legacy_profile_url(online_id): {PROFILE_KEY: {ACCOUNT_ID_KEY: account_id}}
            for online_id, account_id in account_ids.items()
        }
        self._routes = {
            MY_ACCOUNT_URL: {ACCOUNT_ID_KEY: self.own_account_id},
            profiles_url(self.own_account_id): {ONLINE_ID_KEY: own_online_id},
            **legacy_profiles,
            **(routes or {}),
        }
        self.post_response: dict = {}
        self.post_calls: list[tuple[str, dict]] = []
        self.patch_calls: list[tuple[str, dict]] = []
        self.delete_calls: list[str] = []
        self.put_calls: list[str] = []

    async def get(self, url, params=None, headers=None):
        return FakeResponse(self._routes[url])

    async def post(self, url, json=None, data=None, params=None, headers=None):
        self.post_calls.append((url, json or {}))
        return FakeResponse(self.post_response)

    async def patch(self, url, json=None, headers=None):
        self.patch_calls.append((url, json or {}))
        return FakeResponse()

    async def put(self, url, headers=None):
        self.put_calls.append(url)
        return FakeResponse()

    async def delete(self, url, headers=None):
        self.delete_calls.append(url)
        return FakeResponse()

    async def run_with_reauth(self, operation):
        return await operation()


class FakePinnedAccountRepository:
    async def get_pinned_account_id(self, identity_sub):
        return None

    async def pin(self, identity_sub, psn_account_id):
        raise AssertionError("mutations never pin a test account")


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


def _service(session, *, linked_account_id=None, allow_friend_writes=True, allow_chat_writes=True):
    link = FakeLink(
        linked_account_id or session.own_account_id,
        allow_friend_writes=allow_friend_writes,
        allow_chat_writes=allow_chat_writes,
    )
    return _guarded(session, link)


def _guarded(session, link):
    guard = MutationGuard(new_identity_sub(), FakePinnedAccountRepository(), links=FakeLinkReader(link))
    return MutationService(session, guard)


def _member_of(group_id):
    return {my_chat_groups_url(): {GROUPS_KEY: [{GROUP_ID_KEY: group_id}]}}


def _standing(account_id, relation):
    return {friendship_summary_url(account_id): {FRIEND_RELATION_KEY: relation}}


async def test_create_group_rejected_when_not_the_linked_account():
    session = FakeSession()
    service = _service(session, linked_account_id=new_account_id())

    with pytest.raises(MutationNotAllowedError):
        await service.create_group(account_ids=[new_account_id()])

    assert session.post_calls == []


async def test_create_group_rejected_when_chat_writes_not_consented():
    session = FakeSession()
    service = _service(session, allow_chat_writes=False)

    with pytest.raises(MutationNotAllowedError, match=CHAT_WRITES):
        await service.create_group(account_ids=[new_account_id()])

    assert session.post_calls == []


async def test_friend_mutation_rejected_when_only_chat_writes_consented():
    session = FakeSession()
    service = _service(session, allow_friend_writes=False, allow_chat_writes=True)

    with pytest.raises(MutationNotAllowedError, match=FRIEND_WRITES):
        await service.accept_friend(account_id=new_account_id())

    assert session.put_calls == []


async def test_mutation_rejected_when_no_psn_account_is_linked():
    session = FakeSession()
    service = _guarded(session, None)

    with pytest.raises(MutationNotAllowedError, match="No PSN account is linked"):
        await service.create_group(account_ids=[new_account_id()])

    assert session.post_calls == []


async def test_create_group_posts_the_invitees_to_the_collection_so_psn_allocates_a_new_group():
    member_account_id, allocated_group_id = new_account_id(), new_group_id()
    session = FakeSession()
    session.post_response = {GROUP_ID_KEY: allocated_group_id}

    group_id = await _service(session).create_group(account_ids=[member_account_id])

    assert group_id == allocated_group_id
    assert session.post_calls == [(groups_url(), {INVITEES_KEY: [{ACCOUNT_ID_KEY: member_account_id}]})]


async def test_rename_group_sends_patch():
    group_id, name = new_group_id(), new_game_title()
    session = FakeSession()

    await _service(session).rename_group(group_id, name)

    assert session.patch_calls == [(group_url(group_id), {GROUP_NAME_KEY: {VALUE_KEY: name}})]


async def test_send_message_maps_response():
    message_uid = new_opaque_token()
    created_at = new_utc_instant().replace(microsecond=0)
    session = FakeSession()
    session.post_response = {MESSAGE_UID_KEY: message_uid, CREATED_TIMESTAMP_KEY: int(created_at.timestamp()) * 1000}

    sent = await _service(session).send_message(new_group_id(), lowercase_token())

    assert sent.message_uid == message_uid
    assert sent.created_at == created_at.isoformat()


async def test_invite_to_group_posts_the_resolved_account_ids_to_the_named_group():
    group_id, invitee_online_id, invitee_account_id = new_group_id(), new_online_id(), new_account_id()
    session = FakeSession(account_ids_by_online_id={invitee_online_id: invitee_account_id})

    await _service(session).invite_to_group(group_id, online_ids=[invitee_online_id])

    expected_body = {INVITEES_KEY: [{ACCOUNT_ID_KEY: invitee_account_id}]}
    assert session.post_calls == [(group_invitees_url(group_id), expected_body)]


async def test_invite_to_group_returns_the_group_psn_put_the_invitee_in():
    """Recorded live: inviting into a DM answered with a different, newly allocated groupId."""
    allocated_group_id = new_group_id()
    session = FakeSession()
    session.post_response = {GROUP_ID_KEY: allocated_group_id}

    resulting_group_id = await _service(session).invite_to_group(new_group_id(), account_ids=[new_account_id()])

    assert resulting_group_id == allocated_group_id


async def test_kick_from_group_sends_delete():
    group_id, member_account_id = new_group_id(), new_account_id()
    session = FakeSession()

    await _service(session).kick_from_group(group_id, account_id=member_account_id)

    assert session.delete_calls == [group_member_url(group_id, member_account_id)]


async def test_leave_group_sends_delete_for_me():
    group_id = new_group_id()
    session = FakeSession(routes=_member_of(group_id))

    left = await _service(session).leave_group(group_id)

    assert left is True
    assert session.delete_calls == [group_member_url(group_id, SELF_PATH_ID)]


async def test_leaving_a_group_the_caller_is_not_in_sends_nothing():
    session = FakeSession(routes=_member_of(new_group_id()))

    left = await _service(session).leave_group(new_group_id())

    assert left is False
    assert session.delete_calls == []


async def test_accepting_a_pending_request_matches_the_online_id_case_insensitively():
    requester_account_id, requester_online_id = new_account_id(), new_online_id()
    session = FakeSession(
        routes={
            received_requests_url(): {RECEIVED_REQUESTS_KEY: [{ACCOUNT_ID_KEY: requester_account_id}]},
            profiles_url(requester_account_id): {ONLINE_ID_KEY: requester_online_id},
        }
    )

    await _service(session).accept_friend_request(requester_online_id.upper())

    assert session.put_calls == [friend_url(requester_account_id)]


async def test_accepting_with_no_pending_request_raises_and_puts_nothing():
    session = FakeSession(routes={received_requests_url(): {RECEIVED_REQUESTS_KEY: []}})

    with pytest.raises(NoPendingFriendRequestError):
        await _service(session).accept_friend_request(new_online_id())

    assert session.put_calls == []


async def test_removing_a_friend_that_is_not_one_sends_nothing():
    account_id = new_account_id()
    session = FakeSession(routes=_standing(account_id, NO_FRIEND_RELATION))

    removed = await _service(session).remove_friend(account_id=account_id)

    assert removed is False
    assert session.delete_calls == []


async def test_send_friend_request_puts_the_resolved_account_like_accept():
    target_online_id, target_account_id = new_online_id(), new_account_id()
    session = FakeSession(account_ids_by_online_id={target_online_id: target_account_id})

    await _service(session).send_friend_request(target_online_id)

    assert session.put_calls == [friend_url(target_account_id)]


async def test_accept_friend_sends_put():
    account_id = new_account_id()
    session = FakeSession()

    await _service(session).accept_friend(account_id=account_id)

    assert session.put_calls == [friend_url(account_id)]


async def test_remove_friend_sends_delete():
    account_id = new_account_id()
    session = FakeSession(routes=_standing(account_id, lowercase_token()))

    removed = await _service(session).remove_friend(account_id=account_id)

    assert removed is True
    assert session.delete_calls == [friend_url(account_id)]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda service: service.rename_group(new_group_id(), new_game_title()),
        lambda service: service.send_message(new_group_id(), lowercase_token()),
        lambda service: service.kick_from_group(new_group_id(), account_id=new_account_id()),
        lambda service: service.leave_group(new_group_id()),
        lambda service: service.accept_friend(account_id=new_account_id()),
        lambda service: service.accept_friend_request(new_online_id()),
        lambda service: service.send_friend_request(new_online_id()),
        lambda service: service.remove_friend(account_id=new_account_id()),
    ],
)
async def test_every_mutation_checks_the_live_account_before_acting(mutation):
    session = FakeSession()
    service = _service(session, linked_account_id=new_account_id())

    with pytest.raises(MutationNotAllowedError):
        await mutation(service)

    assert (session.post_calls, session.patch_calls, session.delete_calls, session.put_calls) == ([], [], [], [])
