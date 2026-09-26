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

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient

from audit_fakes import RecordingAuditRepository
from authz_constants import ALLOWED_PATH_PARAMETERS
from curator import (
    catalog_routes,
    collections_routes,
    consoles_routes,
    devices_routes,
    enrichment_keys_routes,
    enrichment_routes,
    identity_routes,
    library_routes,
    me_routes,
    measured_sizes_routes,
    preferences_routes,
    presence_routes,
    profile_routes,
    ps_plus_routes,
    psn_routes,
    refresh_schedules_routes,
    social_routes,
    storage_devices_routes,
    trophy_routes,
)
from curator.app import create_app
from curator.deps import require_bearer
from curator.http_headers import BEARER_SCHEME, WWW_AUTHENTICATE_HEADER
from curator.me_routes import MeResponse
from curator.persistence.crypto import TokenCrypto
from curator.persistence.repository import LinkRecord
from curator.psn_routes import LinkRequest
from curator.token_validation import AUTHORITY_UNAVAILABLE_DETAIL, AuthorityUnavailableError, TokenClaims
from test_routes import (
    FakeAgentFactory,
    FakeLibraryRepository,
    FakeRefreshSchedulesRepository,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _build,
    _claims,
    _make_settings,
)

_BEARER_REQUIRED_HANDLERS = [
    me_routes.me,
    me_routes.delete_me,
    me_routes.get_my_actions,
    psn_routes.psn_link,
    psn_routes.psn_unlink,
    catalog_routes.genre_vocabulary_drift,
    catalog_routes.backfill_catalog,
    catalog_routes.walk_ps_plus_catalog,
    enrichment_routes.start_enrichment_run,
    enrichment_routes.cancel_enrichment_run,
    enrichment_routes.get_latest_enrichment_run,
    enrichment_routes.get_enrichment_run_status,
    library_routes.manual_add_candidates,
    library_routes.search_store_for_manual_add,
    library_routes.add_manual_game,
    library_routes.remove_manual_game,
    library_routes.get_library_genres,
    library_routes.get_library,
    library_routes.hide_game,
    library_routes.unhide_game,
    library_routes.refresh_library,
    library_routes.get_library_refresh_status,
    collections_routes.preview_collection,
    collections_routes.save_definition,
    collections_routes.list_definitions,
    collections_routes.list_followed_collections,
    collections_routes.get_definition,
    collections_routes.get_definition_items,
    collections_routes.remove_definition_item,
    collections_routes.update_definition,
    collections_routes.set_visibility,
    collections_routes.delete_definition,
    collections_routes.follow_definition,
    collections_routes.unfollow_definition,
    collections_routes.run_definition,
    consoles_routes.create_console,
    consoles_routes.list_consoles,
    consoles_routes.get_console,
    consoles_routes.update_console,
    consoles_routes.delete_console,
    consoles_routes.link_console_device,
    consoles_routes.unlink_console_device,
    consoles_routes.get_console_installs,
    consoles_routes.set_console_install,
    storage_devices_routes.create_storage_device,
    storage_devices_routes.list_storage_devices,
    storage_devices_routes.get_storage_device,
    storage_devices_routes.update_storage_device,
    storage_devices_routes.delete_storage_device,
    storage_devices_routes.attach_storage_device,
    storage_devices_routes.detach_storage_device,
    storage_devices_routes.get_storage_device_installs,
    storage_devices_routes.set_storage_device_install,
    measured_sizes_routes.list_measured_sizes,
    measured_sizes_routes.set_measured_size,
    trophy_routes.get_trophy_summary,
    trophy_routes.get_trophy_titles,
    trophy_routes.get_title_trophies,
    trophy_routes.get_trophy_groups,
    preferences_routes.get_psn_preferences,
    preferences_routes.set_psn_preferences,
    identity_routes.get_identity,
    presence_routes.get_presence,
    devices_routes.get_devices,
    enrichment_keys_routes.get_enrichment_key_status,
    enrichment_keys_routes.set_enrichment_key,
    enrichment_keys_routes.delete_enrichment_key,
    profile_routes.get_my_profile_settings,
    profile_routes.set_my_profile_settings,
    profile_routes.list_profile_link_sites,
    profile_routes.get_my_profile_links,
    profile_routes.set_my_profile_link,
    profile_routes.delete_my_profile_link,
    profile_routes.get_user_profile,
    profile_routes.follow_user,
    profile_routes.unfollow_user,
    profile_routes.get_followers,
    profile_routes.get_following,
    profile_routes.get_user_library_genres,
    profile_routes.get_user_library,
    profile_routes.get_user_collections,
    refresh_schedules_routes.get_refresh_schedule,
    refresh_schedules_routes.set_refresh_schedule,
    refresh_schedules_routes.delete_refresh_schedule,
    ps_plus_routes.get_ps_plus_rotation,
    ps_plus_routes.get_ps_plus_rotation_summary,
    social_routes.list_friend_requests,
    social_routes.send_friend_request,
    social_routes.accept_friend,
    social_routes.remove_friend,
    social_routes.create_chat_group,
    social_routes.rename_chat_group,
    social_routes.invite_to_chat_group,
    social_routes.leave_chat_group,
]


def _dependency_calls(dependant):
    """Every callable in one route's dependency tree, the endpoint itself included."""
    yield dependant.call
    for sub_dependant in dependant.dependencies:
        yield from _dependency_calls(sub_dependant)


def _effective_routes(app):
    """Every route the app dispatches to, with its included prefix and dependencies applied.

    Since FastAPI 0.137 ``app.routes`` holds one ``_IncludedRouter`` per ``include_router`` call rather than
    the routes themselves, so iterating it directly sees no API route at all -- and a detector built on it
    passes with nothing to check. ``iter_route_contexts`` is the public walk over the effective routes.
    """
    return [context.route for context in iter_route_contexts(app.routes)]


def _reaches_require_bearer(route) -> bool:
    dependant = getattr(route, "dependant", None)
    return dependant is not None and any(call is require_bearer for call in _dependency_calls(dependant))


def _unlisted_bearer_routes(app) -> list[str]:
    listed = set(_BEARER_REQUIRED_HANDLERS)
    return [
        f"{'/'.join(sorted(route.methods))} {route.path}"
        for route in _effective_routes(app)
        if _reaches_require_bearer(route) and route.endpoint not in listed
    ]


def _request_line_for(app, handler) -> tuple[str, str]:
    """The one method ``handler`` answers, and its path with every parameter filled by a generated value.

    The path comes from the route declaration itself (``url_path_for``), so a test names the handler and
    never restates the URL; the generated parameters are never judged, because every listed route refuses
    the request before its body runs.
    """
    route = next(route for route in _effective_routes(app) if getattr(route, "endpoint", None) is handler)
    (method,) = route.methods
    parameters = {name: uuid.uuid4().hex for name in route.param_convertors}
    return method, app.url_path_for(handler.__name__, **parameters)


def _path_parameter_names(app) -> set[str]:
    return {name for route in _effective_routes(app) for name in getattr(route, "param_convertors", {})}


def test_every_route_behind_require_bearer_is_listed_in_bearer_required_handlers():
    """``_BEARER_REQUIRED_HANDLERS`` is hand-maintained and feeds three ``parametrize`` decorators, so a
    protected route missing from it is never asserted at all and the suite still passes. This is the check
    that discriminates: it asks the app which routes actually reach
    :func:`~curator.deps.require_bearer` -- directly or through ``require_verified_caller``/
    ``require_admin`` -- and fails naming any the list does not cover.

    ``optional_bearer`` routes are excluded for free rather than by an exception list: it calls
    ``require_bearer`` from its own body, so it never appears in the dependency graph, which is the same
    reason those routes answer anonymously.

    The list stays a hand-written policy rather than a list derived from the app, so a route that loses its
    ``require_bearer`` dependency still fails the three tests below instead of silently leaving them.
    """
    client, *_ = _build()

    assert _unlisted_bearer_routes(client.app) == []


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


@pytest.mark.parametrize("handler", _BEARER_REQUIRED_HANDLERS, ids=lambda handler: handler.__name__)
def test_bearer_required_routes_reject_missing_authorization_header(handler):
    client, *_ = _build()
    method, path = _request_line_for(client.app, handler)

    response = client.request(method, path)

    assert response.status_code == 401


@pytest.mark.parametrize("handler", _BEARER_REQUIRED_HANDLERS, ids=lambda handler: handler.__name__)
def test_bearer_required_routes_reject_garbage_token(handler):
    client, *_ = _build()
    method, path = _request_line_for(client.app, handler)

    response = client.request(method, path, headers=_bearer(uuid.uuid4().hex))

    assert response.status_code == 401
    assert response.headers.get(WWW_AUTHENTICATE_HEADER) == BEARER_SCHEME


class UnreachableAuthorityValidator:
    """Stands in for ``JwtValidator`` while Identity is down: reaches no verdict on any token at all."""

    def validate(self, token: str) -> TokenClaims:
        raise AuthorityUnavailableError(AUTHORITY_UNAVAILABLE_DETAIL)


@pytest.mark.parametrize("handler", _BEARER_REQUIRED_HANDLERS, ids=lambda handler: handler.__name__)
def test_bearer_required_routes_answer_503_when_identity_cannot_be_reached(handler):
    client, *_ = _build(token_validator=UnreachableAuthorityValidator())
    method, path = _request_line_for(client.app, handler)

    response = client.request(method, path, headers=_bearer(uuid.uuid4().hex))

    assert response.status_code == 503
    assert WWW_AUTHENTICATE_HEADER not in response.headers
    assert response.json()["detail"] == AUTHORITY_UNAVAILABLE_DETAIL


def test_cross_user_isolation_between_two_established_callers():
    repo = RecordingRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
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
        audit_repository=RecordingAuditRepository(),
    )
    app.state.library_repository = FakeLibraryRepository()
    app.state.refresh_schedules_repository = FakeRefreshSchedulesRepository()
    client = TestClient(app)
    me_path = app.url_path_for(me_routes.me.__name__)
    link_path = app.url_path_for(psn_routes.psn_link.__name__)
    relink_npsso = uuid.uuid4().hex

    me_response = client.get(me_path, headers=_bearer("token-a"))
    assert me_response.status_code == 200
    assert repo.delete_calls == []

    baseline = len(repo.all_subs_seen)

    me_body = MeResponse.model_validate(me_response.json())
    assert me_body.sub == "sub-a"
    assert me_body.linked is True
    assert repo.links["sub-b"].psn_account_id == "psn-account-b"

    delete_response = client.delete(link_path, headers=_bearer("token-a"))
    assert delete_response.status_code == 204
    assert repo.delete_calls == ["sub-a"]
    assert "sub-b" in repo.links
    assert repo.links["sub-b"].psn_account_id == "psn-account-b"
    assert repo.links["sub-b"].access_token_expires_at == datetime(2026, 1, 1, 2, tzinfo=timezone.utc)
    assert repo.links["sub-b"].refresh_token_expires_at == datetime(2026, 2, 1, 2, tzinfo=timezone.utc)

    agent_factory.account_id = "psn-account-a-relinked"
    link_response = client.post(
        link_path, json=LinkRequest(npsso=relink_npsso).model_dump(), headers=_bearer("token-a")
    )
    assert link_response.status_code == 200
    assert agent_factory.calls[-1] == ("sub-a", relink_npsso)
    assert repo.set_link_account_calls[-1] == ("sub-a", "psn-account-a-relinked")

    assert repo.links["sub-b"].psn_account_id == "psn-account-b"
    assert repo.links["sub-b"].access_token_expires_at == datetime(2026, 1, 1, 2, tzinfo=timezone.utc)

    subs_touched_by_a = set(repo.all_subs_seen[baseline:])
    assert subs_touched_by_a == {"sub-a"}


def test_no_route_exposes_a_caller_suppliable_user_identifier_path_parameter():
    """No route path parameter may be a "target user" identifier (a ``{sub}``-shaped segment letting one
    user name another's data) -- every route still keys identity exclusively off the validated token's own
    ``sub``. Every other parameter names a *resource* (a console, collection definition, storage device,
    game, chat group, PSN title, online id, platform, enrichment provider, job run, share slug or profile
    link site), never a user. ``consoles_routes`` and its siblings re-check the resource's ownership against
    the caller's own ``sub`` before acting (see ``test_consoles_routes.py``); ``trophy_routes`` needs no such
    check because ``np_communication_id`` only ever selects *which title* to read the caller's own trophy
    data for -- there is no cross-user data reachable through it.

    ``{sub}`` (``curator.profile_routes``'s ``/users/{sub}/...`` family) is the one deliberate exception
    that *does* name another user's account, on purpose -- see that module's docstring and
    ``curator.deps``'s module docstring for the full rationale (viewer-B-looks-at-owner-A's-public-profile,
    always using B's own stored PSN session, never A's).

    The set is compared for equality, so a new path parameter fails here until someone decides which kind
    it names, and a walk that saw no route (the ``app.routes`` blindness above) fails rather than passing
    with nothing checked.
    """
    client, *_ = _build()

    assert _path_parameter_names(client.app) == ALLOWED_PATH_PARAMETERS
