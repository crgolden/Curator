"""Structural authorization tests: prove the "bearer tokens gate everything, subs never cross" property
that ``curator.deps.require_bearer``/``require_verified_caller`` and ``curator.psn_routes`` claim in their
module docstrings, rather than just spot-checking individual status codes.

Reuses ``test_routes``'s hand-written fakes (``FakeRepository``, ``FakeAgentFactory``,
``FakeTokenValidator``, ``_build``, ``_make_settings``, ``_claims``, ``_bearer``) instead of duplicating
them -- pytest's rootdir-relative import inserts ``tests/`` onto ``sys.path``, so a bare
``from test_routes import ...`` resolves the sibling test module the same way ``test_routes.py`` resolves
``curator.*``. No ``unittest.mock`` anywhere, matching the persistence- and route-layer test style.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from curator.persistence.repository import LinkRecord
from test_routes import (
    FakeAgentFactory,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _build,
    _claims,
    _make_settings,
)

_BEARER_REQUIRED_ROUTES = [
    ("get", "/me", {}),
    ("post", "/psn/link", {"json": {"npsso": "whatever"}}),
    ("delete", "/psn/link", {}),
    ("get", "/trophies/summary", {}),
    ("get", "/trophies/titles", {}),
    ("get", "/trophies/titles/NPWR1", {"params": {"platform": "PS5"}}),
    ("get", "/trophies/titles/NPWR1/groups", {"params": {"platform": "PS5"}}),
    ("get", "/me/psn-preferences", {}),
    (
        "put",
        "/me/psn-preferences",
        {
            "json": {
                "harvest_trophies": True,
                "harvest_identity": True,
                "harvest_presence": True,
                "harvest_devices": True,
            }
        },
    ),
    ("get", "/identity", {}),
    ("get", "/presence", {}),
    ("get", "/devices", {}),
    ("get", "/me/profile-settings", {}),
    (
        "put",
        "/me/profile-settings",
        {
            "json": {
                "is_public": True,
                "show_library": True,
                "show_collections": True,
                "show_trophies": True,
                "show_identity": True,
            }
        },
    ),
    ("get", "/users/sub-x/profile", {}),
    ("post", "/users/sub-x/follow", {}),
    ("delete", "/users/sub-x/follow", {}),
    ("get", "/users/sub-x/followers", {}),
    ("get", "/users/sub-x/following", {}),
    ("get", "/users/sub-x/library", {}),
    ("get", "/users/sub-x/collections", {}),
]


class RecordingRepository(FakeRepository):
    """``FakeRepository`` plus an unfiltered log of every ``sub`` any method was called with.

    Used to prove a request made as one user never causes the repository to be consulted about another
    user's data -- the sub-tracking is generic (every method that takes a ``sub`` is wrapped) rather than
    hand-picking which methods "should" matter.
    """

    def __init__(self) -> None:
        super().__init__()
        self.all_subs_seen: list[str] = []

    async def upsert_user(self, sub):
        self.all_subs_seen.append(sub)
        return await super().upsert_user(sub)

    async def touch_login(self, sub):
        self.all_subs_seen.append(sub)
        return await super().touch_login(sub)

    async def get_link(self, sub):
        self.all_subs_seen.append(sub)
        return await super().get_link(sub)

    async def upsert_link(
        self, sub, token_response_enc, access_token_expires_at, refresh_token_expires_at, psn_account_id=None
    ):
        self.all_subs_seen.append(sub)
        return await super().upsert_link(
            sub,
            token_response_enc,
            access_token_expires_at,
            refresh_token_expires_at,
            psn_account_id=psn_account_id,
        )

    async def set_link_account(self, sub, psn_account_id):
        self.all_subs_seen.append(sub)
        return await super().set_link_account(sub, psn_account_id)

    async def touch_link_verified(self, sub):
        self.all_subs_seen.append(sub)
        return await super().touch_link_verified(sub)

    async def delete_link(self, sub):
        self.all_subs_seen.append(sub)
        return await super().delete_link(sub)


def _seed_custom_link(repo: RecordingRepository, crypto: TokenCrypto, sub: str, account_id: str, hour: int) -> None:
    """Seed a link with values distinguishable per-user (unlike ``test_routes._seed_link``'s fixed
    timestamps), so isolation between two users' rows is actually observable in assertions."""
    encrypted = crypto.encrypt(f'{{"access_token": "AT-{sub}", "refresh_token": "RT-{sub}"}}'.encode())
    repo.links[sub] = LinkRecord(
        psn_account_id=account_id,
        token_response_enc=encrypted,
        access_token_expires_at=datetime(2026, 1, 1, hour, tzinfo=timezone.utc),
        refresh_token_expires_at=datetime(2026, 2, 1, hour, tzinfo=timezone.utc),
        linked_at=datetime(2026, 1, 1, hour, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, hour, tzinfo=timezone.utc),
        last_verified_at=datetime(2026, 1, 1, hour, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------------------------------
# Every bearer-required route rejects missing/garbage tokens.
# ---------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "kwargs"), _BEARER_REQUIRED_ROUTES)
def test_bearer_required_routes_reject_missing_authorization_header(method, path, kwargs):
    client, *_ = _build()
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "kwargs"), _BEARER_REQUIRED_ROUTES)
def test_bearer_required_routes_reject_garbage_token(method, path, kwargs):
    client, *_ = _build()
    response = getattr(client, method)(path, headers=_bearer("garbage-not-a-real-token"), **kwargs)
    assert response.status_code == 401


# ---------------------------------------------------------------------------------------------------
# Cross-user isolation.
# ---------------------------------------------------------------------------------------------------


def test_cross_user_isolation_between_two_established_callers():
    repo = RecordingRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_custom_link(repo, crypto, "sub-a", account_id="psn-account-a", hour=1)
    _seed_custom_link(repo, crypto, "sub-b", account_id="psn-account-b", hour=2)

    agent_factory = FakeAgentFactory(repo, crypto)
    agent_factory.email_info = ("usera@example.com", True)
    validator = FakeTokenValidator()
    validator.register(
        "token-a",
        _claims(sub="sub-a", email="usera@example.com", iat=datetime(2026, 1, 1, 2, tzinfo=timezone.utc)),
    )
    app = create_app(
        _make_settings(),
        repository=repo,
        token_crypto=crypto,
        agent_factory=agent_factory,
        token_validator=validator,
    )
    client = TestClient(app)

    # Establish A's identity. (Reverify-on-token matches emails, so A's pre-seeded link survives.)
    me_response = client.get("/me", headers=_bearer("token-a"))
    assert me_response.status_code == 200
    assert repo.delete_calls == []

    # From here on, only calls made as A are in scope for the isolation assertion below.
    baseline = len(repo.all_subs_seen)

    # A's /me reflects only A's link, not B's.
    me_body = me_response.json()
    assert me_body["sub"] == "sub-a"
    assert me_body["linked"] is True
    assert repo.links["sub-b"].psn_account_id == "psn-account-b"

    # A's DELETE deletes only A's rows; B's link record is untouched in the fake.
    delete_response = client.delete("/psn/link", headers=_bearer("token-a"))
    assert delete_response.status_code == 204
    assert repo.delete_calls == ["sub-a"]
    assert "sub-b" in repo.links
    assert repo.links["sub-b"].psn_account_id == "psn-account-b"
    assert repo.links["sub-b"].access_token_expires_at == datetime(2026, 1, 1, 2, tzinfo=timezone.utc)
    assert repo.links["sub-b"].refresh_token_expires_at == datetime(2026, 2, 1, 2, tzinfo=timezone.utc)

    # A's POST /psn/link (re-linking after her own delete) writes only under A's sub.
    agent_factory.account_id = "psn-account-a-relinked"
    link_response = client.post("/psn/link", json={"npsso": "a-new-npsso"}, headers=_bearer("token-a"))
    assert link_response.status_code == 200
    assert agent_factory.calls[-1] == ("sub-a", "a-new-npsso")
    assert repo.set_link_account_calls[-1] == ("sub-a", "psn-account-a-relinked")

    # B's row was never touched by any of A's requests.
    assert repo.links["sub-b"].psn_account_id == "psn-account-b"
    assert repo.links["sub-b"].access_token_expires_at == datetime(2026, 1, 1, 2, tzinfo=timezone.utc)

    # The repository was never consulted about B's sub at any point during A's requests.
    subs_touched_by_a = set(repo.all_subs_seen[baseline:])
    assert subs_touched_by_a == {"sub-a"}


# ---------------------------------------------------------------------------------------------------
# No route accepts a caller-supplied target-user identifier.
# ---------------------------------------------------------------------------------------------------


_ALLOWED_PATH_PARAMETERS = {"console_id", "game_id", "np_communication_id", "sub"}


def test_no_route_exposes_a_caller_suppliable_user_identifier_path_parameter():
    """No route path parameter may be a "target user" identifier (a ``{sub}``-shaped segment letting one
    user name another's data) -- every route still keys identity exclusively off the validated token's own
    ``sub``. ``{console_id}``/``{game_id}`` (``PUT /consoles/{console_id}/installs/{game_id}``) and
    ``{np_communication_id}`` (``GET /trophies/titles/{np_communication_id}`` and its ``/groups``
    sibling) are the sole *resource*-naming exceptions: they name a console/game/PSN title, not a user.
    ``consoles_routes`` re-checks the resource's ownership against the caller's own ``sub`` before acting
    (see ``test_consoles_routes.py``); ``trophy_routes`` needs no such check because
    ``np_communication_id`` only ever selects *which title* to read the caller's own trophy data for --
    there is no cross-user data reachable through it.

    ``{sub}`` (``curator.profile_routes``'s ``/users/{sub}/...`` family) is the one deliberate exception
    that *does* name another user's account, on purpose -- see that module's docstring and
    ``curator.deps``'s module docstring for the full rationale (viewer-B-looks-at-owner-A's-public-profile,
    always using B's own stored PSN session, never A's).
    """
    client, *_ = _build()
    app = client.app

    route_paths = [route.path for route in app.routes if hasattr(route, "path")]
    assert route_paths, "expected at least one route to introspect"
    for path in route_paths:
        for segment in path.split("/"):
            if segment.startswith("{") and segment.endswith("}"):
                name = segment[1:-1]
                assert name in _ALLOWED_PATH_PARAMETERS, f"route {path!r} exposes an unexpected path parameter {name!r}"
