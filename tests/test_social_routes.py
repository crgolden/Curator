"""Tests for the PSN social/chat write routes -- create_app wired with FakeRepository plus a fake
mutation_service_factory and audit repository (the same DI-seam style as test_preferences_routes.py).

The guard's own predicate is covered in test_psn_safety.py; these tests cover how the routes translate a
missing link, a withheld consent flag and a guard refusal into status codes, and what they write to
``account_action_log``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from audit_fakes import RecordingAuditRepository
from curator import social_routes
from curator.app import create_app
from curator.audit.repository import (
    ACTION_CHAT_GROUP_CREATED,
    ACTION_CHAT_GROUP_RENAMED,
    ACTION_CHAT_MEMBERSHIP_CHANGED,
    ACTION_FRIEND_ADDED,
    ACTION_FRIEND_REMOVED,
    ACTION_FRIEND_REQUEST_SENT,
    OUTCOME_COMPLETED,
    OUTCOME_FAILED,
    OUTCOME_STARTED,
)
from curator.deps import HARVEST_IDENTITY, PREFERENCE_NOT_LINKED_DETAIL, preference_disabled_detail
from curator.persistence.crypto import TokenCrypto
from curator.psn.errors import MutationNotAllowedError, NoPendingFriendRequestError, PsnAuthError
from curator.psn.models import SocialUser
from curator.psn.safety import FRIEND_WRITES
from curator.social_routes import (
    NO_PENDING_REQUEST_DETAIL,
    CreateGroupRequest,
    CreateGroupResponse,
    FriendRequestResponse,
    FriendRequestsResponse,
    InviteToGroupRequest,
    InviteToGroupResponse,
    RenameGroupRequest,
    invited_detail,
    left_detail,
    nothing_sent_detail,
)
from test_routes import (
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _path,
    _seed_link,
)
from test_values import (
    lowercase_token,
    new_account_id,
    new_email_address,
    new_group_id,
    new_identity_sub,
    new_online_id,
    new_opaque_token,
)

SUB = new_identity_sub()
EMAIL = new_email_address()
TOKEN = new_opaque_token()


class FakeMutationService:
    """Records every call; ``friends`` and ``groups`` model the PSN state a destructive call reads first.
    ``history`` is the audit fake whose rows each call snapshots into ``history_at_call``."""

    def __init__(self, *, raises=None, group_id=None, friends=(), groups=()):
        self._raises = raises
        self._group_id = group_id or new_group_id()
        self.friends = set(friends)
        self.groups = set(groups)
        self.calls: list[tuple[str, tuple, dict]] = []
        self.history: RecordingAuditRepository | None = None
        self.history_at_call: list[list[tuple[str, str]]] = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.history is not None:
            self.history_at_call.append(self.history.outcomes)
        if self._raises is not None:
            raise self._raises

    async def accept_friend_request(self, online_id):
        self._record("accept_friend_request", online_id=online_id)

    async def send_friend_request(self, online_id):
        self._record("send_friend_request", online_id=online_id)

    async def remove_friend(self, online_id=None, account_id=None):
        self._record("remove_friend", online_id=online_id)
        if online_id in self.friends:
            self.friends.discard(online_id)
            return True
        return False

    async def create_group(self, online_ids=None, account_ids=None):
        self._record("create_group", online_ids=online_ids, account_ids=account_ids)
        return self._group_id

    async def rename_group(self, group_id, name):
        self._record("rename_group", group_id, name)

    async def invite_to_group(self, group_id, online_ids=None, account_ids=None):
        self._record("invite_to_group", group_id, online_ids=online_ids, account_ids=account_ids)
        return self._group_id

    async def leave_group(self, group_id):
        self._record("leave_group", group_id)
        if group_id in self.groups:
            self.groups.discard(group_id)
            return True
        return False


class FakeSocialClient:
    def __init__(self, pending=(), *, raises=None):
        self._pending = list(pending)
        self._raises = raises

    async def friend_requests(self):
        if self._raises is not None:
            raise self._raises
        return list(self._pending)


def _build(*, service=None, unlinked_factory=False, social_client=None, linked=True, **flags):
    repository = FakeRepository()
    if linked:
        _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), SUB, **flags)

    validator = FakeTokenValidator()
    validator.register(TOKEN, _claims(sub=SUB, email=EMAIL))
    service = service if service is not None else FakeMutationService()
    audit = RecordingAuditRepository()
    service.history = audit

    async def factory(sub):
        if unlinked_factory:
            raise RuntimeError(sub)
        return service

    async def social_factory(sub):
        if unlinked_factory or social_client is None:
            raise RuntimeError(sub)
        return social_client

    app = create_app(
        _make_settings(),
        repository=repository,
        token_validator=validator,
        audit_repository=audit,
        mutation_service_factory=factory,
        social_client_factory=social_factory,
    )
    return TestClient(app), service, audit


def _friend_path(client, online_id: str) -> str:
    return _path(client, social_routes.accept_friend, online_id=online_id)


def _unfriend_path(client, online_id: str) -> str:
    return _path(client, social_routes.remove_friend, online_id=online_id)


def _friend_request_path(client, online_id: str) -> str:
    return _path(client, social_routes.send_friend_request, online_id=online_id)


def _friend_requests_path(client) -> str:
    return _path(client, social_routes.list_friend_requests)


def _groups_path(client) -> str:
    return _path(client, social_routes.create_chat_group)


def _group_path(client, group_id: str) -> str:
    return _path(client, social_routes.rename_chat_group, group_id=group_id)


def _invitees_path(client, group_id: str) -> str:
    return _path(client, social_routes.invite_to_chat_group, group_id=group_id)


def _membership_path(client, group_id: str) -> str:
    return _path(client, social_routes.leave_chat_group, group_id=group_id)


def test_accept_friend_without_a_psn_link_is_404():
    client, service, _ = _build(linked=False)

    response = client.put(_friend_path(client, new_online_id()), headers=_bearer(TOKEN))

    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL
    assert service.calls == []


def test_accept_friend_without_friend_write_consent_is_403():
    client, service, _ = _build(allow_friend_writes=False, allow_chat_writes=True)

    response = client.put(_friend_path(client, new_online_id()), headers=_bearer(TOKEN))

    assert response.status_code == 403
    assert response.json()["detail"] == preference_disabled_detail(FRIEND_WRITES)
    assert service.calls == []


def test_sending_a_friend_request_without_consent_is_403_and_writes_nothing():
    client, service, audit = _build(allow_friend_writes=False)

    response = client.post(_friend_request_path(client, new_online_id()), headers=_bearer(TOKEN))

    assert response.status_code == 403
    assert response.json()["detail"] == preference_disabled_detail(FRIEND_WRITES)
    assert service.calls == []
    assert audit.rows == [], "a consent refusal is decided from the database before any history row or PSN call"


def test_accepting_when_no_request_is_pending_is_409_and_sends_nothing_to_psn():
    requester = new_online_id()
    refusing = FakeMutationService(raises=NoPendingFriendRequestError(requester))
    client, service, audit = _build(service=refusing, allow_friend_writes=True)

    response = client.put(_friend_path(client, requester), headers=_bearer(TOKEN))

    assert response.status_code == 409
    assert response.json()["detail"] == NO_PENDING_REQUEST_DETAIL
    assert [call[0] for call in service.calls] == ["accept_friend_request"]
    assert audit.outcomes == [(ACTION_FRIEND_ADDED, OUTCOME_FAILED)]


def test_chat_write_consent_does_not_authorize_a_friend_write():
    client, service, _ = _build(allow_chat_writes=True, allow_friend_writes=False)
    group_body = CreateGroupRequest(online_ids=[new_online_id()]).model_dump()

    response = client.post(_groups_path(client), json=group_body, headers=_bearer(TOKEN))
    assert response.status_code == 200

    response = client.delete(_unfriend_path(client, new_online_id()), headers=_bearer(TOKEN))
    assert response.status_code == 403
    assert response.json()["detail"] == preference_disabled_detail(FRIEND_WRITES)
    assert [call[0] for call in service.calls] == ["create_group"]


def test_guard_refusal_inside_the_service_is_403():
    refusal = lowercase_token()
    refusing = FakeMutationService(raises=MutationNotAllowedError(refusal))
    client, _, audit = _build(service=refusing, allow_friend_writes=True)

    response = client.put(_friend_path(client, new_online_id()), headers=_bearer(TOKEN))

    assert response.status_code == 403
    assert response.json()["detail"] == refusal
    assert audit.outcomes == [(ACTION_FRIEND_ADDED, OUTCOME_FAILED)]


def test_expired_psn_token_is_401():
    client, _, audit = _build(
        service=FakeMutationService(raises=PsnAuthError(new_opaque_token())), allow_friend_writes=True
    )

    response = client.put(_friend_path(client, new_online_id()), headers=_bearer(TOKEN))

    assert response.status_code == 401
    assert audit.outcomes == [(ACTION_FRIEND_ADDED, OUTCOME_FAILED)]


def test_factory_reporting_no_link_is_404():
    client, _, _ = _build(unlinked_factory=True, allow_friend_writes=True)

    response = client.put(_friend_path(client, new_online_id()), headers=_bearer(TOKEN))

    assert response.status_code == 404
    assert response.json()["detail"] == PREFERENCE_NOT_LINKED_DETAIL


def test_accepting_a_pending_request_logs_a_friend_added():
    requester = new_online_id()
    client, service, audit = _build(allow_friend_writes=True)

    response = client.put(_friend_path(client, requester), headers=_bearer(TOKEN))

    assert response.status_code == 204
    assert service.calls == [("accept_friend_request", (), {"online_id": requester})]
    assert audit.entries == [(SUB, ACTION_FRIEND_ADDED, requester)]


def test_sending_a_friend_request_logs_its_own_action():
    target = new_online_id()
    client, service, audit = _build(allow_friend_writes=True)

    response = client.post(_friend_request_path(client, target), headers=_bearer(TOKEN))

    assert response.status_code == 204
    assert service.calls == [("send_friend_request", (), {"online_id": target})]
    assert audit.entries == [(SUB, ACTION_FRIEND_REQUEST_SENT, target)]


def test_removing_a_friend_twice_writes_to_psn_once_and_answers_204_both_times():
    friend = new_online_id()
    service = FakeMutationService(friends=[friend])
    client, _, audit = _build(service=service, allow_friend_writes=True)

    first = client.delete(_unfriend_path(client, friend), headers=_bearer(TOKEN))
    second = client.delete(_unfriend_path(client, friend), headers=_bearer(TOKEN))

    assert (first.status_code, second.status_code) == (204, 204)
    assert audit.entries == [
        (SUB, ACTION_FRIEND_REMOVED, friend),
        (SUB, ACTION_FRIEND_REMOVED, nothing_sent_detail(friend)),
    ], "both attempts used the token, and the second says it sent nothing"


def test_listing_friend_requests_needs_identity_harvesting():
    client, _, _ = _build(social_client=FakeSocialClient(), harvest_identity=False)

    response = client.get(_friend_requests_path(client), headers=_bearer(TOKEN))

    assert response.status_code == 403
    assert response.json()["detail"] == preference_disabled_detail(HARVEST_IDENTITY)


def test_listing_friend_requests_is_a_live_proxy():
    requester = SocialUser(account_id=new_account_id(), online_id=new_online_id())
    client, _, _ = _build(social_client=FakeSocialClient([requester]), harvest_identity=True)

    response = client.get(_friend_requests_path(client), headers=_bearer(TOKEN))

    assert response.status_code == 200
    assert FriendRequestsResponse.model_validate(response.json()) == FriendRequestsResponse(
        requests=[FriendRequestResponse(online_id=requester.online_id, account_id=requester.account_id)]
    )


def test_listing_friend_requests_with_a_rejected_token_is_401():
    rejecting = FakeSocialClient(raises=PsnAuthError(new_opaque_token()))
    client, _, _ = _build(social_client=rejecting, harvest_identity=True)

    assert client.get(_friend_requests_path(client), headers=_bearer(TOKEN)).status_code == 401


def test_create_chat_group_returns_and_logs_the_group_id():
    peer, account = new_online_id(), new_account_id()
    service = FakeMutationService()
    client, _, audit = _build(service=service, allow_chat_writes=True)

    response = client.post(
        _groups_path(client),
        json=CreateGroupRequest(online_ids=[peer], account_ids=[account]).model_dump(),
        headers=_bearer(TOKEN),
    )

    assert response.status_code == 200
    group_id = CreateGroupResponse.model_validate(response.json()).group_id
    assert service.calls == [("create_group", (), {"online_ids": [peer], "account_ids": [account]})]
    assert audit.entries == [(SUB, ACTION_CHAT_GROUP_CREATED, group_id)]


def test_renaming_a_chat_group_logs_its_own_action():
    group_id = new_group_id()
    new_name = lowercase_token()
    client, service, audit = _build(allow_chat_writes=True)

    response = client.patch(
        _group_path(client, group_id), json=RenameGroupRequest(name=new_name).model_dump(), headers=_bearer(TOKEN)
    )

    assert response.status_code == 204
    assert service.calls == [("rename_group", (group_id, new_name), {})]
    assert audit.entries == [(SUB, ACTION_CHAT_GROUP_RENAMED, group_id)]


def test_renaming_a_chat_group_to_a_blank_name_is_422():
    client, service, _ = _build(allow_chat_writes=True)

    response = client.patch(
        _group_path(client, new_group_id()),
        json=RenameGroupRequest.model_construct(name="").model_dump(),
        headers=_bearer(TOKEN),
    )

    assert response.status_code == 422
    assert service.calls == []


def test_inviting_to_a_chat_group_reports_where_psn_put_the_invitee_and_logs_that_group():
    """PSN answers an invite into a two-person DM by allocating a new group, so the response and the audit
    detail carry PSN's group id, not the path's."""
    group_id, resulting_group_id, peer = new_group_id(), new_group_id(), new_online_id()
    service = FakeMutationService(group_id=resulting_group_id)
    client, service, audit = _build(service=service, allow_chat_writes=True)

    response = client.post(
        _invitees_path(client, group_id),
        json=InviteToGroupRequest(online_ids=[peer]).model_dump(),
        headers=_bearer(TOKEN),
    )

    assert response.status_code == 200
    assert InviteToGroupResponse.model_validate(response.json()) == InviteToGroupResponse(group_id=resulting_group_id)
    assert service.calls == [("invite_to_group", (group_id,), {"online_ids": [peer], "account_ids": []})]
    assert audit.entries == [(SUB, ACTION_CHAT_MEMBERSHIP_CHANGED, invited_detail(resulting_group_id, 1))]


def test_leave_chat_group_logs_a_membership_change():
    group_id = new_group_id()
    service = FakeMutationService(groups=[group_id])
    client, _, audit = _build(service=service, allow_chat_writes=True)

    response = client.delete(_membership_path(client, group_id), headers=_bearer(TOKEN))

    assert response.status_code == 204
    assert service.calls == [("leave_group", (group_id,), {})]
    assert audit.entries == [(SUB, ACTION_CHAT_MEMBERSHIP_CHANGED, left_detail(group_id))]


def test_leaving_a_group_twice_writes_to_psn_once():
    group_id = new_group_id()
    service = FakeMutationService(groups=[group_id])
    client, _, audit = _build(service=service, allow_chat_writes=True)

    client.delete(_membership_path(client, group_id), headers=_bearer(TOKEN))
    second = client.delete(_membership_path(client, group_id), headers=_bearer(TOKEN))

    assert second.status_code == 204
    assert audit.entries == [
        (SUB, ACTION_CHAT_MEMBERSHIP_CHANGED, left_detail(group_id)),
        (SUB, ACTION_CHAT_MEMBERSHIP_CHANGED, nothing_sent_detail(group_id)),
    ]


def test_the_history_row_is_started_before_the_mutation_reaches_psn():
    requester = new_online_id()
    client, service, audit = _build(allow_friend_writes=True)

    client.put(_friend_path(client, requester), headers=_bearer(TOKEN))

    assert service.history_at_call == [[(ACTION_FRIEND_ADDED, OUTCOME_STARTED)]]
    assert audit.outcomes == [(ACTION_FRIEND_ADDED, OUTCOME_COMPLETED)]


def test_a_history_row_that_cannot_be_written_stops_the_mutation_before_psn():
    group_id = new_group_id()
    client, service, audit = _build(service=FakeMutationService(groups=[group_id]), allow_chat_writes=True)
    audit.begin_error = RuntimeError(new_group_id())

    with pytest.raises(RuntimeError):
        client.delete(_membership_path(client, group_id), headers=_bearer(TOKEN))

    assert service.calls == []
    assert audit.rows == []


def test_an_outcome_that_cannot_be_written_fails_the_request_and_leaves_the_attempt_recorded():
    group_id = new_group_id()
    client, service, audit = _build(service=FakeMutationService(groups=[group_id]), allow_chat_writes=True)
    audit.finish_error = RuntimeError(new_group_id())

    with pytest.raises(RuntimeError):
        client.delete(_membership_path(client, group_id), headers=_bearer(TOKEN))

    assert [call[0] for call in service.calls] == ["leave_group"]
    assert audit.outcomes == [(ACTION_CHAT_MEMBERSHIP_CHANGED, OUTCOME_STARTED)]


def test_social_routes_require_a_bearer_token():
    group_id = new_group_id()
    online_id = new_online_id()
    client, service, _ = _build(allow_friend_writes=True, allow_chat_writes=True)

    for response in (
        client.put(_friend_path(client, online_id)),
        client.delete(_unfriend_path(client, online_id)),
        client.post(_friend_request_path(client, online_id)),
        client.get(_friend_requests_path(client)),
        client.post(_groups_path(client), json=CreateGroupRequest().model_dump()),
        client.patch(_group_path(client, group_id), json=RenameGroupRequest(name=lowercase_token()).model_dump()),
        client.post(_invitees_path(client, group_id), json=InviteToGroupRequest().model_dump()),
        client.delete(_membership_path(client, group_id)),
    ):
        assert response.status_code == 401

    assert service.calls == []
