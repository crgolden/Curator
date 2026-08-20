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
from curator.psn.errors import MutationNotAllowedError, PsnAuthError
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

GROUP_ID = "ba08b67ca0b044b7688a29abdc884f37b5dd47cd-215"


class FakeAuditRepository:
    def __init__(self):
        self.entries: list[tuple[str, str, str | None]] = []

    async def log(self, identity_sub, action, detail=None):
        self.entries.append((identity_sub, action, detail))


class FakeMutationService:
    def __init__(self, *, raises=None, group_id="new-group"):
        self._raises = raises
        self._group_id = group_id
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self._raises is not None:
            raise self._raises

    async def accept_friend(self, online_id=None, account_id=None):
        self._record("accept_friend", online_id=online_id)

    async def remove_friend(self, online_id=None, account_id=None):
        self._record("remove_friend", online_id=online_id)

    async def create_group(self, online_ids=None, account_ids=None):
        self._record("create_group", online_ids=online_ids, account_ids=account_ids)
        return self._group_id

    async def leave_group(self, group_id):
        self._record("leave_group", group_id)


def _build(*, service=None, unlinked_factory=False, **flags):
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

    app = create_app(
        _make_settings(),
        repository=repository,
        token_validator=validator,
        audit_repository=audit,
        mutation_service_factory=factory,
    )
    return TestClient(app), service, audit


def test_add_friend_without_a_psn_link_is_404():
    client, service, _ = _build(linked=False)

    response = client.put("/me/friends/SomeOnlineId", headers=_bearer("valid-token"))

    assert response.status_code == 404
    assert service.calls == []


def test_add_friend_without_friend_write_consent_is_403():
    client, service, _ = _build(allow_friend_writes=False, allow_chat_writes=True)

    response = client.put("/me/friends/SomeOnlineId", headers=_bearer("valid-token"))

    assert response.status_code == 403
    assert "allow_friend_writes" in response.json()["detail"]
    assert service.calls == []


def test_chat_write_consent_does_not_authorize_a_friend_write():
    client, service, _ = _build(allow_chat_writes=True, allow_friend_writes=False)

    response = client.post("/me/chat/groups", json={"online_ids": ["peer-one"]}, headers=_bearer("valid-token"))
    assert response.status_code == 200

    response = client.delete("/me/friends/SomeOnlineId", headers=_bearer("valid-token"))
    assert response.status_code == 403
    assert [call[0] for call in service.calls] == ["create_group"]


def test_guard_refusal_inside_the_service_is_403():
    refusing = FakeMutationService(raises=MutationNotAllowedError("Daily PSN change limit reached (50 in 24 hours)."))
    client, _, audit = _build(service=refusing, allow_friend_writes=True)

    response = client.put("/me/friends/SomeOnlineId", headers=_bearer("valid-token"))

    assert response.status_code == 403
    assert "Daily PSN change limit" in response.json()["detail"]
    assert audit.entries == []


def test_expired_psn_token_is_401():
    client, _, audit = _build(service=FakeMutationService(raises=PsnAuthError("token dead")), allow_friend_writes=True)

    response = client.put("/me/friends/SomeOnlineId", headers=_bearer("valid-token"))

    assert response.status_code == 401
    assert audit.entries == []


def test_factory_reporting_no_link_is_404():
    client, _, _ = _build(unlinked_factory=True, allow_friend_writes=True)

    response = client.put("/me/friends/SomeOnlineId", headers=_bearer("valid-token"))

    assert response.status_code == 404


def test_add_friend_logs_the_mutation():
    client, service, audit = _build(allow_friend_writes=True)

    response = client.put("/me/friends/SomeOnlineId", headers=_bearer("valid-token"))

    assert response.status_code == 204
    assert service.calls == [("accept_friend", (), {"online_id": "SomeOnlineId"})]
    assert audit.entries == [(SUB, "friend_added", "SomeOnlineId")]


def test_remove_friend_logs_the_mutation():
    client, service, audit = _build(allow_friend_writes=True)

    response = client.delete("/me/friends/SomeOnlineId", headers=_bearer("valid-token"))

    assert response.status_code == 204
    assert service.calls == [("remove_friend", (), {"online_id": "SomeOnlineId"})]
    assert audit.entries == [(SUB, "friend_removed", "SomeOnlineId")]


def test_create_chat_group_returns_and_logs_the_group_id():
    client, service, audit = _build(allow_chat_writes=True)

    response = client.post(
        "/me/chat/groups", json={"online_ids": ["peer-one"], "account_ids": ["9"]}, headers=_bearer("valid-token")
    )

    assert response.status_code == 200
    assert response.json() == {"group_id": "new-group"}
    assert service.calls == [("create_group", (), {"online_ids": ["peer-one"], "account_ids": ["9"]})]
    assert audit.entries == [(SUB, "chat_group_created", "new-group")]


def test_inviting_to_a_chat_group_has_no_route():
    client, service, _ = _build(allow_chat_writes=True)

    response = client.post(
        "/me/chat/groups/" + GROUP_ID + "/invitees", json={"online_ids": ["peer-one"]}, headers=_bearer("valid-token")
    )

    assert response.status_code == 404
    assert service.calls == []


def test_leave_chat_group_logs_a_membership_change():
    client, service, audit = _build(allow_chat_writes=True)

    response = client.delete("/me/chat/groups/" + GROUP_ID + "/members/me", headers=_bearer("valid-token"))

    assert response.status_code == 204
    assert service.calls == [("leave_group", (GROUP_ID,), {})]
    assert audit.entries == [(SUB, "chat_membership_changed", f"{GROUP_ID} left")]


def test_a_failed_audit_write_does_not_fail_the_mutation():
    client, _, audit = _build(allow_chat_writes=True)

    async def failing_log(identity_sub, action, detail=None):
        raise RuntimeError("audit table unreachable")

    audit.log = failing_log

    response = client.delete("/me/chat/groups/" + GROUP_ID + "/members/me", headers=_bearer("valid-token"))

    assert response.status_code == 204


def test_social_routes_require_a_bearer_token():
    client, service, _ = _build(allow_friend_writes=True, allow_chat_writes=True)

    for response in (
        client.put("/me/friends/x"),
        client.delete("/me/friends/x"),
        client.post("/me/chat/groups", json={}),
        client.delete("/me/chat/groups/" + GROUP_ID + "/members/me"),
    ):
        assert response.status_code == 401

    assert service.calls == []
