"""Tests for the PSN social/chat write routes -- create_app wired with FakeRepository plus a fake
mutation_service_factory and audit repository (the same DI-seam style as test_preferences_routes.py).

The guard's own predicate is covered in test_psn_safety.py; these tests cover how the routes translate a
missing link, a withheld consent flag and a guard refusal into status codes, and what they write to
``account_action_log``.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from curator.psn.errors import MutationNotAllowedError, NoPendingFriendRequestError, PsnAuthError
from curator.psn.models import SocialUser
from curator.social_routes import NO_PENDING_REQUEST_DETAIL
from test_routes import (
    EMAIL,
    SUB,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _seed_link,
)
from test_values import new_account_id, new_group_id, new_online_id


class FakeAuditRepository:
    def __init__(self):
        self.entries: list[tuple[str, str, str | None]] = []

    async def log(self, identity_sub, action, detail=None):
        self.entries.append((identity_sub, action, detail))


class FakeMutationService:
    """Records every call; ``friends`` and ``groups`` model the PSN state a destructive call reads first."""

    def __init__(self, *, raises=None, group_id=None, friends=(), groups=()):
        self._raises = raises
        self._group_id = group_id or new_group_id()
        self.friends = set(friends)
        self.groups = set(groups)
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
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


def _build(*, service=None, unlinked_factory=False, social_client=None, **flags):
    repository = FakeRepository()
    if flags.pop("linked", True):
        _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), SUB, **flags)

    validator = FakeTokenValidator()
    validator.register("valid-token", _claims(sub=SUB, email=EMAIL))
    service = service if service is not None else FakeMutationService()
    audit = FakeAuditRepository()

    async def factory(sub):
        if unlinked_factory:
            raise RuntimeError("no link")
        return service

    async def social_factory(sub):
        if unlinked_factory or social_client is None:
            raise RuntimeError("no link")
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


def test_accept_friend_without_a_psn_link_is_404():
    client, service, _ = _build(linked=False)

    response = client.put(f"/me/friends/{new_online_id()}", headers=_bearer("valid-token"))

    assert response.status_code == 404
    assert service.calls == []


def test_accept_friend_without_friend_write_consent_is_403():
    client, service, _ = _build(allow_friend_writes=False, allow_chat_writes=True)

    response = client.put(f"/me/friends/{new_online_id()}", headers=_bearer("valid-token"))

    assert response.status_code == 403
    assert "allow_friend_writes" in response.json()["detail"]
    assert service.calls == []


def test_sending_a_friend_request_without_consent_is_403_and_writes_nothing():
    client, service, audit = _build(allow_friend_writes=False)

    response = client.post(f"/me/friend-requests/{new_online_id()}", headers=_bearer("valid-token"))

    assert response.status_code == 403
    assert service.calls == []
    assert audit.entries == []


def test_accepting_when_no_request_is_pending_is_409_and_sends_nothing_to_psn():
    requester = new_online_id()
    refusing = FakeMutationService(raises=NoPendingFriendRequestError(f"{requester} has not sent a friend request."))
    client, service, audit = _build(service=refusing, allow_friend_writes=True)

    response = client.put(f"/me/friends/{requester}", headers=_bearer("valid-token"))

    assert response.status_code == 409
    assert response.json()["detail"] == NO_PENDING_REQUEST_DETAIL
    assert [call[0] for call in service.calls] == ["accept_friend_request"]
    assert audit.entries == []


def test_chat_write_consent_does_not_authorize_a_friend_write():
    client, service, _ = _build(allow_chat_writes=True, allow_friend_writes=False)

    response = client.post("/me/chat/groups", json={"online_ids": [new_online_id()]}, headers=_bearer("valid-token"))
    assert response.status_code == 200

    response = client.delete(f"/me/friends/{new_online_id()}", headers=_bearer("valid-token"))
    assert response.status_code == 403
    assert [call[0] for call in service.calls] == ["create_group"]


def test_guard_refusal_inside_the_service_is_403():
    refusing = FakeMutationService(raises=MutationNotAllowedError("Daily PSN change limit reached (50 in 24 hours)."))
    client, _, audit = _build(service=refusing, allow_friend_writes=True)

    response = client.put(f"/me/friends/{new_online_id()}", headers=_bearer("valid-token"))

    assert response.status_code == 403
    assert "Daily PSN change limit" in response.json()["detail"]
    assert audit.entries == []


def test_expired_psn_token_is_401():
    client, _, audit = _build(service=FakeMutationService(raises=PsnAuthError("token dead")), allow_friend_writes=True)

    response = client.put(f"/me/friends/{new_online_id()}", headers=_bearer("valid-token"))

    assert response.status_code == 401
    assert audit.entries == []


def test_factory_reporting_no_link_is_404():
    client, _, _ = _build(unlinked_factory=True, allow_friend_writes=True)

    response = client.put(f"/me/friends/{new_online_id()}", headers=_bearer("valid-token"))

    assert response.status_code == 404


def test_accepting_a_pending_request_logs_a_friend_added():
    requester = new_online_id()
    client, service, audit = _build(allow_friend_writes=True)

    response = client.put(f"/me/friends/{requester}", headers=_bearer("valid-token"))

    assert response.status_code == 204
    assert service.calls == [("accept_friend_request", (), {"online_id": requester})]
    assert audit.entries == [(SUB, "friend_added", requester)]


def test_sending_a_friend_request_logs_its_own_action():
    target = new_online_id()
    client, service, audit = _build(allow_friend_writes=True)

    response = client.post(f"/me/friend-requests/{target}", headers=_bearer("valid-token"))

    assert response.status_code == 204
    assert service.calls == [("send_friend_request", (), {"online_id": target})]
    assert audit.entries == [(SUB, "friend_request_sent", target)]


def test_removing_a_friend_twice_writes_to_psn_once_and_answers_204_both_times():
    friend = new_online_id()
    service = FakeMutationService(friends=[friend])
    client, _, audit = _build(service=service, allow_friend_writes=True)

    first = client.delete(f"/me/friends/{friend}", headers=_bearer("valid-token"))
    second = client.delete(f"/me/friends/{friend}", headers=_bearer("valid-token"))

    assert (first.status_code, second.status_code) == (204, 204)
    assert audit.entries == [(SUB, "friend_removed", friend)], "only the write that changed PSN is logged"


def test_listing_friend_requests_needs_identity_harvesting():
    client, _, _ = _build(social_client=FakeSocialClient(), harvest_identity=False)

    response = client.get("/me/friend-requests", headers=_bearer("valid-token"))

    assert response.status_code == 403


def test_listing_friend_requests_is_a_live_proxy():
    requester = SocialUser(account_id=new_account_id(), online_id=new_online_id())
    client, _, _ = _build(social_client=FakeSocialClient([requester]), harvest_identity=True)

    response = client.get("/me/friend-requests", headers=_bearer("valid-token"))

    assert response.status_code == 200
    assert response.json() == {"requests": [{"online_id": requester.online_id, "account_id": requester.account_id}]}


def test_listing_friend_requests_with_a_rejected_token_is_401():
    client, _, _ = _build(social_client=FakeSocialClient(raises=PsnAuthError("dead")), harvest_identity=True)

    assert client.get("/me/friend-requests", headers=_bearer("valid-token")).status_code == 401


def test_create_chat_group_returns_and_logs_the_group_id():
    peer, account = new_online_id(), new_account_id()
    service = FakeMutationService()
    client, _, audit = _build(service=service, allow_chat_writes=True)

    response = client.post(
        "/me/chat/groups", json={"online_ids": [peer], "account_ids": [account]}, headers=_bearer("valid-token")
    )

    assert response.status_code == 200
    group_id = response.json()["group_id"]
    assert service.calls == [("create_group", (), {"online_ids": [peer], "account_ids": [account]})]
    assert audit.entries == [(SUB, "chat_group_created", group_id)]


def test_renaming_a_chat_group_logs_its_own_action():
    group_id = new_group_id()
    client, service, audit = _build(allow_chat_writes=True)

    response = client.patch(f"/me/chat/groups/{group_id}", json={"name": "Raid night"}, headers=_bearer("valid-token"))

    assert response.status_code == 204
    assert service.calls == [("rename_group", (group_id, "Raid night"), {})]
    assert audit.entries == [(SUB, "chat_group_renamed", group_id)]


def test_renaming_a_chat_group_to_a_blank_name_is_422():
    client, service, _ = _build(allow_chat_writes=True)

    response = client.patch(f"/me/chat/groups/{new_group_id()}", json={"name": ""}, headers=_bearer("valid-token"))

    assert response.status_code == 422
    assert service.calls == []


def test_inviting_to_a_chat_group_reports_where_psn_put_the_invitee_and_logs_that_group():
    """PSN answers an invite into a two-person DM by allocating a new group, so the response and the audit
    detail carry PSN's group id, not the path's."""
    group_id, resulting_group_id, peer = new_group_id(), new_group_id(), new_online_id()
    service = FakeMutationService(group_id=resulting_group_id)
    client, service, audit = _build(service=service, allow_chat_writes=True)

    response = client.post(
        f"/me/chat/groups/{group_id}/invitees", json={"online_ids": [peer]}, headers=_bearer("valid-token")
    )

    assert response.status_code == 200
    assert response.json() == {"group_id": resulting_group_id}
    assert service.calls == [("invite_to_group", (group_id,), {"online_ids": [peer], "account_ids": []})]
    assert audit.entries == [(SUB, "chat_membership_changed", f"{resulting_group_id} invited 1")]


def test_leave_chat_group_logs_a_membership_change():
    group_id = new_group_id()
    service = FakeMutationService(groups=[group_id])
    client, _, audit = _build(service=service, allow_chat_writes=True)

    response = client.delete(f"/me/chat/groups/{group_id}/members/me", headers=_bearer("valid-token"))

    assert response.status_code == 204
    assert service.calls == [("leave_group", (group_id,), {})]
    assert audit.entries == [(SUB, "chat_membership_changed", f"{group_id} left")]


def test_leaving_a_group_twice_writes_to_psn_once():
    group_id = new_group_id()
    service = FakeMutationService(groups=[group_id])
    client, _, audit = _build(service=service, allow_chat_writes=True)

    client.delete(f"/me/chat/groups/{group_id}/members/me", headers=_bearer("valid-token"))
    second = client.delete(f"/me/chat/groups/{group_id}/members/me", headers=_bearer("valid-token"))

    assert second.status_code == 204
    assert len(audit.entries) == 1


def test_a_failed_audit_write_does_not_fail_the_mutation():
    group_id = new_group_id()
    client, _, audit = _build(service=FakeMutationService(groups=[group_id]), allow_chat_writes=True)

    async def failing_log(identity_sub, action, detail=None):
        raise RuntimeError("audit table unreachable")

    audit.log = failing_log

    response = client.delete(f"/me/chat/groups/{group_id}/members/me", headers=_bearer("valid-token"))

    assert response.status_code == 204


def test_social_routes_require_a_bearer_token():
    group_id = new_group_id()
    client, service, _ = _build(allow_friend_writes=True, allow_chat_writes=True)

    for response in (
        client.put("/me/friends/x"),
        client.delete("/me/friends/x"),
        client.post("/me/friend-requests/x"),
        client.get("/me/friend-requests"),
        client.post("/me/chat/groups", json={}),
        client.patch(f"/me/chat/groups/{group_id}", json={"name": "x"}),
        client.post(f"/me/chat/groups/{group_id}/invitees", json={}),
        client.delete(f"/me/chat/groups/{group_id}/members/me"),
    ):
        assert response.status_code == 401

    assert service.calls == []
