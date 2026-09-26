"""Tests for /me/profile-settings, /users/{sub}/profile, /users/{sub}/follow, /users/{sub}/followers,
/users/{sub}/following, /users/{sub}/library, and /users/{sub}/collections.

Big enough to warrant its own file (unlike trophy/identity/enrichment-keys, which share test_routes.py).
Every collaborator is a hand-written fake -- no unittest.mock, matching the rest of this suite. The fake
TrophyClientFactory/SocialClient stand-ins return canned data keyed by the ``account_id`` argument they
were called with, so tests can assert B's request resolved A's ``account_id``, never B's own.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

from curator import profile_routes
from curator.app import create_app
from curator.audit.repository import ACTION_FOLLOWED, ACTION_PROFILE_LOOKUP, ACTION_UNFOLLOWED, OUTCOME_COMPLETED
from curator.collections.collection_spec import CAPACITY_FILL_KIND, FILTER_LIST_KIND
from curator.collections.repository import (
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC,
    VISIBILITY_UNLISTED,
    CollectionDefinition,
)
from curator.library.repository import HIDDEN_EXCLUDE
from curator.library_routes import LibraryGenresResponse
from curator.persistence.crypto import TokenCrypto
from curator.persistence.follow_repository import FollowEdge
from curator.persistence.profile_link_repository import (
    HANDLE_PLACEHOLDER,
    ProfileLink,
    ProfileLinkSite,
    profile_link_url,
)
from curator.persistence.profile_repository import ProfileSettings
from curator.persistence.repository import LinkRecord
from curator.profile_routes import (
    FollowListResponse,
    ProfileDefinitionResponse,
    ProfileIdentityResponse,
    ProfileLibraryGameResponse,
    ProfileLibraryPageResponse,
    ProfileLinkRequest,
    ProfileLinkResponse,
    ProfileSettingsRequest,
    ProfileSettingsResponse,
    ProfileTrophySummaryResponse,
    PublicProfileResponse,
)
from curator.psn.errors import PsnAuthError
from curator.psn.models import TrophyCounts, TrophySummary
from curator.psn.title_platform import PS3, PS5
from curator.query_params import LIMIT_PARAM, OFFSET_PARAM
from curator.trophy_routes import TrophyCountsResponse
from test_routes import (
    FakeAuditRepository,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _path,
)
from test_values import (
    lowercase_token,
    new_account_id,
    new_console_id,
    new_cover_image_url,
    new_definition_id,
    new_email_address,
    new_game_id,
    new_game_title,
    new_identity_sub,
    new_online_id,
    new_opaque_token,
    new_positive_count,
    new_small_count,
    token_from_first_half_of_alphabet,
    token_from_second_half_of_alphabet,
)

SUB_A = new_identity_sub()
SUB_B = new_identity_sub()
TOKEN_A = new_opaque_token()
TOKEN_B = new_opaque_token()

_DEFINITIONS = TypeAdapter(list[ProfileDefinitionResponse])
_LINKS = TypeAdapter(list[ProfileLinkResponse])

_HIDDEN = ProfileSettings(
    is_public=False, show_library=False, show_collections=False, show_trophies=False, show_identity=False
)


def _settings(**flags) -> ProfileSettings:
    return replace(_HIDDEN, **flags)


class FakeProfileRepository:
    def __init__(self) -> None:
        self.settings: dict[str, ProfileSettings] = {}
        self.upsert_calls: list[tuple] = []

    async def get_settings(self, sub: str) -> ProfileSettings:
        return self.settings.get(sub, _HIDDEN)

    async def upsert_settings(
        self, sub: str, *, is_public, show_library, show_collections, show_trophies, show_identity
    ) -> None:
        self.settings[sub] = ProfileSettings(
            is_public=is_public,
            show_library=show_library,
            show_collections=show_collections,
            show_trophies=show_trophies,
            show_identity=show_identity,
        )
        self.upsert_calls.append((sub, is_public, show_library, show_collections, show_trophies, show_identity))


class FakeFollowRepository:
    def __init__(self) -> None:
        self.edges: dict[tuple[str, str], datetime] = {}
        self._next_time = datetime(2026, 1, 1, tzinfo=timezone.utc)

    async def follow(self, follower_sub: str, followed_sub: str) -> None:
        key = (follower_sub, followed_sub)
        if key not in self.edges:
            self.edges[key] = self._next_time
            self._next_time += timedelta(seconds=1)

    async def unfollow(self, follower_sub: str, followed_sub: str) -> bool:
        return self.edges.pop((follower_sub, followed_sub), None) is not None

    async def is_following(self, follower_sub: str, followed_sub: str) -> bool:
        return (follower_sub, followed_sub) in self.edges

    async def follower_count(self, sub: str) -> int:
        return sum(1 for (_f, t) in self.edges if t == sub)

    async def following_count(self, sub: str) -> int:
        return sum(1 for (f, _t) in self.edges if f == sub)

    async def list_followers(self, sub: str, *, limit: int = 100, offset: int = 0) -> list[FollowEdge]:
        items = sorted(
            ((f, ts) for (f, t), ts in self.edges.items() if t == sub), key=lambda item: item[1], reverse=True
        )
        return [FollowEdge(sub=f, followed_at=ts) for f, ts in items[offset : offset + limit]]

    async def list_following(self, sub: str, *, limit: int = 100, offset: int = 0) -> list[FollowEdge]:
        items = sorted(
            ((t, ts) for (f, t), ts in self.edges.items() if f == sub), key=lambda item: item[1], reverse=True
        )
        return [FollowEdge(sub=t, followed_at=ts) for t, ts in items[offset : offset + limit]]


class FakeProfileTrophyClient:
    """Returns a canned TrophySummary keyed by the account_id it was called with, or raises PsnAuthError."""

    def __init__(self, summaries_by_account_id: dict[str, TrophySummary], *, raise_auth_error: bool = False) -> None:
        self._summaries = summaries_by_account_id
        self._raise_auth_error = raise_auth_error
        self.calls: list[str | None] = []

    async def trophy_summary(self, online_id=None, account_id=None):
        self.calls.append(account_id)
        if self._raise_auth_error:
            raise PsnAuthError(account_id)
        return self._summaries[account_id]


class FakeProfileTrophyClientFactory:
    """Records which viewer sub built the client; raises RuntimeError for a sub with no PSN link."""

    def __init__(self) -> None:
        self.linked: dict[str, FakeProfileTrophyClient] = {}
        self.calls: list[str] = []

    async def __call__(self, sub: str):
        self.calls.append(sub)
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(sub)
        return client


class FakeProfileSocialClient:
    """Returns a canned online_id keyed by the account_id it was called with, or raises PsnAuthError."""

    def __init__(self, online_ids_by_account_id: dict[str, str | None], *, raise_auth_error: bool = False) -> None:
        self._online_ids = online_ids_by_account_id
        self._raise_auth_error = raise_auth_error
        self.calls: list[str] = []

    async def online_id(self, account_id: str) -> str | None:
        self.calls.append(account_id)
        if self._raise_auth_error:
            raise PsnAuthError(account_id)
        return self._online_ids.get(account_id)


class FakeProfileSocialClientFactory:
    def __init__(self) -> None:
        self.linked: dict[str, FakeProfileSocialClient] = {}
        self.calls: list[str] = []

    async def __call__(self, sub: str):
        self.calls.append(sub)
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(sub)
        return client


class FakeLibraryRepository:
    def __init__(self, games_by_sub=None) -> None:
        self._games_by_sub = games_by_sub or {}

    async def count_entries(self, identity_sub: str) -> int:
        return len(self._games_by_sub.get(identity_sub, []))

    async def list_entries_with_enrichment(
        self,
        identity_sub: str,
        *,
        search=None,
        genre=None,
        sort="title",
        sort_dir="asc",
        limit=20,
        offset=0,
        hidden=HIDDEN_EXCLUDE,
    ):
        games = self._games_by_sub.get(identity_sub, [])
        return games[offset : offset + limit], len(games)

    async def list_genres(self, identity_sub: str):
        games = self._games_by_sub.get(identity_sub, [])
        return sorted({g.genre for g in games if g.genre is not None})


class FakeLibraryGameView:
    def __init__(
        self,
        game_id=None,
        title=None,
        genre=None,
        rawg_rating=None,
        opencritic_rating=None,
        psn_rating=None,
        psn_product_id=None,
        rawg_enriched=False,
        opencritic_enriched=False,
        psn_enriched=False,
        is_active=True,
        percent_completed=None,
        cover_image_url=None,
        platforms=(),
    ) -> None:
        self.game_id = game_id or new_game_id()
        self.title = title or new_game_title()
        self.genre = genre
        self.rawg_rating = rawg_rating
        self.opencritic_rating = opencritic_rating
        self.psn_rating = psn_rating
        self.psn_product_id = psn_product_id
        self.rawg_enriched = rawg_enriched
        self.opencritic_enriched = opencritic_enriched
        self.psn_enriched = psn_enriched
        self.is_active = is_active
        self.percent_completed = percent_completed
        self.cover_image_url = cover_image_url
        self.platforms = platforms


def _site() -> ProfileLinkSite:
    return ProfileLinkSite(
        site_key=lowercase_token(),
        display_name=new_game_title(),
        url_template=f"https://{lowercase_token()}.com/{HANDLE_PLACEHOLDER}",
        sort_order=1,
    )


def _handle() -> str:
    return lowercase_token(new_small_count() + 3)


class FakeProfileLinkRepository:
    def __init__(self, links_by_sub=None, sites=None) -> None:
        self._links_by_sub = links_by_sub or {}
        self._sites = sites or [_site()]

    async def list_sites(self):
        return self._sites

    async def list_for_user(self, sub: str):
        return self._links_by_sub.get(sub, [])

    async def upsert_link(self, sub: str, site_key: str, handle: str) -> None:
        site = next(s for s in self._sites if s.site_key == site_key)
        link = ProfileLink(
            site_key=site_key,
            display_name=site.display_name,
            handle=handle,
            url=profile_link_url(site.url_template, handle),
        )
        self._links_by_sub[sub] = [x for x in self._links_by_sub.get(sub, []) if x.site_key != site_key] + [link]

    async def delete_link(self, sub: str, site_key: str) -> None:
        self._links_by_sub[sub] = [x for x in self._links_by_sub.get(sub, []) if x.site_key != site_key]


class FakeCollectionsRepository:
    def __init__(self, definitions_by_sub=None) -> None:
        self._definitions_by_sub = definitions_by_sub or {}

    async def count_definitions(self, identity_sub: str, *, public_only: bool = False) -> int:
        definitions = self._definitions_by_sub.get(identity_sub, [])
        if public_only:
            return len([d for d in definitions if d.visibility == VISIBILITY_PUBLIC])
        return len(definitions)

    async def list_definitions(self, identity_sub: str):
        return self._definitions_by_sub.get(identity_sub, [])


def _build(
    *,
    repository=None,
    profile_repository=None,
    profile_link_repository=None,
    follow_repository=None,
    trophy_client_factory=None,
    social_client_factory=None,
    library_repository=None,
    collections_repository=None,
    audit_repository=None,
):
    settings = _make_settings()
    repository = repository if repository is not None else FakeRepository()
    profile_repository = profile_repository if profile_repository is not None else FakeProfileRepository()
    profile_link_repository = (
        profile_link_repository if profile_link_repository is not None else FakeProfileLinkRepository()
    )
    follow_repository = follow_repository if follow_repository is not None else FakeFollowRepository()
    trophy_client_factory = (
        trophy_client_factory if trophy_client_factory is not None else FakeProfileTrophyClientFactory()
    )
    social_client_factory = (
        social_client_factory if social_client_factory is not None else FakeProfileSocialClientFactory()
    )
    library_repository = library_repository if library_repository is not None else FakeLibraryRepository()
    collections_repository = (
        collections_repository if collections_repository is not None else FakeCollectionsRepository()
    )
    audit_repository = audit_repository if audit_repository is not None else FakeAuditRepository()

    validator = FakeTokenValidator()
    validator.register(TOKEN_A, _claims(sub=SUB_A, email=new_email_address()))
    validator.register(TOKEN_B, _claims(sub=SUB_B, email=new_email_address()))

    app = create_app(
        settings,
        repository=repository,
        token_crypto=TokenCrypto(TokenCrypto.generate_key()),
        token_validator=validator,
        profile_repository=profile_repository,
        profile_link_repository=profile_link_repository,
        follow_repository=follow_repository,
        trophy_client_factory=trophy_client_factory,
        social_client_factory=social_client_factory,
        library_repository=library_repository,
        collections_repository=collections_repository,
        audit_repository=audit_repository,
    )
    return (
        TestClient(app),
        repository,
        profile_repository,
        follow_repository,
        trophy_client_factory,
        social_client_factory,
        audit_repository,
    )


def _seed_users(repository: FakeRepository, *subs: str) -> None:
    repository.users.update(subs)


def _profile(client, sub, token) -> PublicProfileResponse:
    response = client.get(_path(client, profile_routes.get_user_profile, sub=sub), headers=_bearer(token))
    return PublicProfileResponse.model_validate(response.json())


def _my_settings(client, token) -> ProfileSettingsResponse:
    response = client.get(_path(client, profile_routes.get_my_profile_settings), headers=_bearer(token))
    return ProfileSettingsResponse.model_validate(response.json())


def _put_settings(client, token, settings: ProfileSettings):
    body = ProfileSettingsRequest(
        is_public=settings.is_public,
        show_library=settings.show_library,
        show_collections=settings.show_collections,
        show_trophies=settings.show_trophies,
        show_identity=settings.show_identity,
    ).model_dump()
    return client.put(_path(client, profile_routes.set_my_profile_settings), json=body, headers=_bearer(token))


def _settings_response(settings: ProfileSettings) -> ProfileSettingsResponse:
    return ProfileSettingsResponse(
        is_public=settings.is_public,
        show_library=settings.show_library,
        show_collections=settings.show_collections,
        show_trophies=settings.show_trophies,
        show_identity=settings.show_identity,
    )


def _put_link(client, token, site_key, handle):
    return client.put(
        _path(client, profile_routes.set_my_profile_link, site_key=site_key),
        json=ProfileLinkRequest(handle=handle).model_dump(),
        headers=_bearer(token),
    )


def _trophy_summary(account_id) -> TrophySummary:
    return TrophySummary(
        level=new_positive_count(),
        progress=new_small_count(),
        tier=new_small_count(),
        earned=TrophyCounts(
            bronze=new_positive_count(),
            silver=new_positive_count(),
            gold=new_positive_count(),
            platinum=new_small_count(),
        ),
        account_id=account_id,
    )


def test_get_my_profile_settings_never_404s_with_no_row():
    client, *_ = _build()

    response = client.get(_path(client, profile_routes.get_my_profile_settings), headers=_bearer(TOKEN_A))

    assert response.status_code == 200
    assert ProfileSettingsResponse.model_validate(response.json()) == _settings_response(_HIDDEN)


def test_put_profile_settings_never_leaks_across_users():
    client, _repo, profile_repository, *_ = _build()
    a_saved = _settings(is_public=True, show_library=True)
    b_saved = _settings(show_collections=True, show_trophies=True, show_identity=True)

    _put_settings(client, TOKEN_A, a_saved)
    _put_settings(client, TOKEN_B, b_saved)

    assert _my_settings(client, TOKEN_A) == _settings_response(a_saved)
    assert _my_settings(client, TOKEN_B) == _settings_response(b_saved)
    assert {call[0] for call in profile_repository.upsert_calls} == {SUB_A, SUB_B}


def test_get_profile_unknown_sub_is_404():
    client, *_ = _build()

    response = client.get(
        _path(client, profile_routes.get_user_profile, sub=new_identity_sub()), headers=_bearer(TOKEN_A)
    )

    assert response.status_code == 404


def test_owner_viewing_own_private_profile_sees_everything():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(
        show_library=True, show_collections=True, show_trophies=True, show_identity=True
    )
    account_id = new_account_id()
    repository.links[SUB_A] = _link(psn_account_id=account_id, harvest_trophies=True, harvest_identity=True)

    trophy_summary = _trophy_summary(account_id)
    trophy_client = FakeProfileTrophyClient({account_id: trophy_summary})
    trophy_factory = FakeProfileTrophyClientFactory()
    trophy_factory.linked[SUB_A] = trophy_client

    online_id = new_online_id()
    social_client = FakeProfileSocialClient({account_id: online_id})
    social_factory = FakeProfileSocialClientFactory()
    social_factory.linked[SUB_A] = social_client

    client, *_ = _build(
        repository=repository,
        profile_repository=profile_repository,
        trophy_client_factory=trophy_factory,
        social_client_factory=social_factory,
    )

    body = _profile(client, SUB_A, TOKEN_A)

    assert body.viewer_is_owner is True
    assert body.psn_account_id == account_id
    assert body.library_visible is True
    assert body.collections_visible is True
    assert body.trophies == ProfileTrophySummaryResponse(
        level=trophy_summary.level,
        tier=trophy_summary.tier,
        earned=TrophyCountsResponse(
            bronze=trophy_summary.earned.bronze,
            silver=trophy_summary.earned.silver,
            gold=trophy_summary.earned.gold,
            platinum=trophy_summary.earned.platinum,
        ),
    )
    assert body.identity == ProfileIdentityResponse(online_id=online_id)
    assert trophy_client.calls == [account_id]
    assert social_client.calls == [account_id]


def test_non_owner_viewing_a_private_profile_sees_only_counts_and_follow_status():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(
        show_library=True, show_collections=True, show_trophies=True, show_identity=True
    )
    repository.links[SUB_A] = _link(psn_account_id=new_account_id(), harvest_trophies=True, harvest_identity=True)

    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, follow_repository=FakeFollowRepository()
    )

    body = _profile(client, SUB_A, TOKEN_B)

    assert body.sub == SUB_A
    assert body.is_public is False
    assert body.viewer_is_owner is False
    assert body.viewer_is_following is False
    assert body.follower_count == 0
    assert body.following_count == 0
    assert body.psn_account_id is None
    assert body.library_visible is False
    assert body.collections_visible is False
    assert body.trophies is None
    assert body.identity is None


def test_profile_body_declares_every_field_it_returns():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    client, *_ = _build(repository=repository)

    response = client.get(_path(client, profile_routes.get_user_profile, sub=SUB_A), headers=_bearer(TOKEN_B))

    assert set(response.json()) == set(PublicProfileResponse.model_fields)


def test_private_profile_withholds_both_counts_from_a_non_owner():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(show_library=True, show_collections=True)
    library_repository = FakeLibraryRepository({SUB_A: [FakeLibraryGameView()]})
    collections_repository = FakeCollectionsRepository({SUB_A: [_definition(visibility=VISIBILITY_PUBLIC)]})

    client, *_ = _build(
        repository=repository,
        profile_repository=profile_repository,
        library_repository=library_repository,
        collections_repository=collections_repository,
    )

    body = _profile(client, SUB_A, TOKEN_B)

    assert body.library_visible is False
    assert body.collections_visible is False
    assert body.library_count is None
    assert body.collections_count is None


def test_owner_sees_real_counts_from_the_same_fixtures_the_viewer_is_denied():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _HIDDEN
    library_repository = FakeLibraryRepository({SUB_A: [FakeLibraryGameView()]})
    collections_repository = FakeCollectionsRepository({SUB_A: [_definition(visibility=VISIBILITY_PUBLIC)]})

    client, *_ = _build(
        repository=repository,
        profile_repository=profile_repository,
        library_repository=library_repository,
        collections_repository=collections_repository,
    )

    body = _profile(client, SUB_A, TOKEN_A)

    assert body.library_count == 1
    assert body.collections_count == 1


def test_collections_count_for_a_non_owner_excludes_private_and_unlisted():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(is_public=True, show_collections=True)
    collections_repository = FakeCollectionsRepository(
        {
            SUB_A: [
                _definition(visibility=VISIBILITY_PUBLIC),
                _definition(visibility=VISIBILITY_UNLISTED),
                _definition(visibility=VISIBILITY_PRIVATE),
            ]
        }
    )

    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, collections_repository=collections_repository
    )

    assert _profile(client, SUB_A, TOKEN_B).collections_count == 1
    assert _profile(client, SUB_A, TOKEN_A).collections_count == 3


def test_library_count_includes_inactive_entries():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(show_library=True)
    library_repository = FakeLibraryRepository(
        {SUB_A: [FakeLibraryGameView(is_active=True), FakeLibraryGameView(is_active=False)]}
    )

    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, library_repository=library_repository
    )

    assert _profile(client, SUB_A, TOKEN_A).library_count == 2


def test_created_at_is_returned_even_to_a_viewer_of_a_private_profile():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _HIDDEN

    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    created_at = asyncio.run(repository.get_created_at(SUB_A))
    assert _profile(client, SUB_A, TOKEN_B).created_at == created_at.isoformat()


def test_trophies_hidden_by_owner_setting_is_owner_only():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(is_public=True)
    repository.links[SUB_A] = _link(psn_account_id=new_account_id(), harvest_trophies=False)

    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    owner_body = _profile(client, SUB_A, TOKEN_A)
    viewer_body = _profile(client, SUB_A, TOKEN_B)

    assert owner_body.trophies is None
    assert owner_body.trophies_hidden_by_owner_setting is True
    assert viewer_body.trophies_hidden_by_owner_setting is False


def test_trophies_hidden_flag_is_false_when_the_owner_has_no_psn_link_at_all():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _HIDDEN

    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    assert _profile(client, SUB_A, TOKEN_A).trophies_hidden_by_owner_setting is False


def test_profile_links_are_withheld_from_a_viewer_of_a_private_profile():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _HIDDEN
    site = _site()
    handle = _handle()
    link = ProfileLink(
        site_key=site.site_key,
        display_name=site.display_name,
        handle=handle,
        url=profile_link_url(site.url_template, handle),
    )
    link_repository = FakeProfileLinkRepository({SUB_A: [link]}, sites=[site])

    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, profile_link_repository=link_repository
    )

    assert _profile(client, SUB_A, TOKEN_B).profile_links == []
    assert _profile(client, SUB_A, TOKEN_A).profile_links[0].url == link.url


def test_setting_a_profile_link_builds_the_url_from_the_site_template():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    site_root = f"https://{lowercase_token()}.com/"
    site = replace(_site(), url_template=f"{site_root}{HANDLE_PLACEHOLDER}")
    handle = _handle()
    client, *_ = _build(repository=repository, profile_link_repository=FakeProfileLinkRepository(sites=[site]))

    response = _put_link(client, TOKEN_A, site.site_key, handle)

    assert response.status_code == 200
    assert ProfileLinkResponse.model_validate(response.json()) == ProfileLinkResponse(
        site_key=site.site_key,
        display_name=site.display_name,
        handle=handle,
        url=f"{site_root}{handle}",
    )


def test_setting_a_profile_link_rejects_a_site_outside_the_allowlist():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    client, *_ = _build(repository=repository)

    response = _put_link(client, TOKEN_A, lowercase_token(), _handle())

    assert response.status_code == 400


@pytest.mark.parametrize(
    "handle",
    [
        "javascript:alert(1)",
        "../../etc/passwd",
        "deeprog?x=1",
        "deep rog",
        "ab",
        "a" * 17,
        "<script>",
    ],
)
def test_setting_a_profile_link_rejects_a_handle_that_is_not_a_handle(handle):
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    site = _site()
    client, *_ = _build(repository=repository, profile_link_repository=FakeProfileLinkRepository(sites=[site]))

    response = _put_link(client, TOKEN_A, site.site_key, handle)

    assert response.status_code == 400


def test_deleting_a_profile_link_is_idempotent():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    site = _site()
    client, *_ = _build(repository=repository, profile_link_repository=FakeProfileLinkRepository(sites=[site]))
    link_path = _path(client, profile_routes.delete_my_profile_link, site_key=site.site_key)

    _put_link(client, TOKEN_A, site.site_key, _handle())

    first = client.delete(link_path, headers=_bearer(TOKEN_A))
    second = client.delete(link_path, headers=_bearer(TOKEN_A))

    assert first.status_code == 204
    assert second.status_code == 204
    links = client.get(_path(client, profile_routes.get_my_profile_links), headers=_bearer(TOKEN_A))
    assert _LINKS.validate_python(links.json()) == []


def test_show_trophies_true_but_harvest_trophies_false_is_none():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(is_public=True, show_trophies=True)
    repository.links[SUB_A] = _link(psn_account_id=new_account_id(), harvest_trophies=False)

    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    response = client.get(_path(client, profile_routes.get_user_profile, sub=SUB_A), headers=_bearer(TOKEN_B))
    assert response.status_code == 200
    assert PublicProfileResponse.model_validate(response.json()).trophies is None


def test_show_and_harvest_trophies_true_but_viewer_has_no_psn_link_is_none_and_still_200():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(is_public=True, show_trophies=True)
    repository.links[SUB_A] = _link(psn_account_id=new_account_id(), harvest_trophies=True)
    trophy_factory = FakeProfileTrophyClientFactory()

    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, trophy_client_factory=trophy_factory
    )

    response = client.get(_path(client, profile_routes.get_user_profile, sub=SUB_A), headers=_bearer(TOKEN_B))
    assert response.status_code == 200
    assert PublicProfileResponse.model_validate(response.json()).trophies is None


def test_cross_user_trophy_lookup_uses_the_targets_account_id_via_the_viewers_own_client():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(is_public=True, show_trophies=True, show_identity=True)
    account_id = new_account_id()
    repository.links[SUB_A] = _link(psn_account_id=account_id, harvest_trophies=True, harvest_identity=True)

    trophy_summary = _trophy_summary(account_id)
    trophy_client = FakeProfileTrophyClient({account_id: trophy_summary})
    trophy_factory = FakeProfileTrophyClientFactory()
    trophy_factory.linked[SUB_B] = trophy_client

    online_id = new_online_id()
    social_client = FakeProfileSocialClient({account_id: online_id})
    social_factory = FakeProfileSocialClientFactory()
    social_factory.linked[SUB_B] = social_client

    client, *_rest, audit = _build(
        repository=repository,
        profile_repository=profile_repository,
        trophy_client_factory=trophy_factory,
        social_client_factory=social_factory,
    )

    body = _profile(client, SUB_A, TOKEN_B)

    assert body.trophies is not None
    assert body.trophies.level == trophy_summary.level
    assert body.identity == ProfileIdentityResponse(online_id=online_id)
    assert trophy_factory.calls == [SUB_B]
    assert trophy_client.calls == [account_id]
    assert social_factory.calls == [SUB_B]
    assert social_client.calls == [account_id]
    assert [(row.identity_sub, row.action, row.outcome) for row in audit.rows] == [
        (SUB_B, ACTION_PROFILE_LOOKUP, OUTCOME_COMPLETED),
        (SUB_B, ACTION_PROFILE_LOOKUP, OUTCOME_COMPLETED),
    ], "each lookup on the viewer's token is recorded against the viewer"


def test_a_cross_user_lookup_is_not_made_when_its_history_row_cannot_be_written():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(is_public=True, show_trophies=True)
    repository.links[SUB_A] = _link(psn_account_id=new_account_id(), harvest_trophies=True)
    trophy_factory = FakeProfileTrophyClientFactory()
    audit = FakeAuditRepository()
    audit.begin_error = RuntimeError(SUB_A)
    client, *_ = _build(
        repository=repository,
        profile_repository=profile_repository,
        trophy_client_factory=trophy_factory,
        audit_repository=audit,
    )

    with pytest.raises(RuntimeError):
        client.get(_path(client, profile_routes.get_user_profile, sub=SUB_A), headers=_bearer(TOKEN_B))

    assert trophy_factory.calls == []


def test_profile_identity_response_has_no_region_field_at_all():
    assert "region" not in ProfileIdentityResponse.model_fields


def test_follow_unknown_sub_is_404():
    client, *_ = _build()

    response = client.post(_path(client, profile_routes.follow_user, sub=new_identity_sub()), headers=_bearer(TOKEN_A))

    assert response.status_code == 404


def test_follow_self_is_400():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    client, *_ = _build(repository=repository)

    response = client.post(_path(client, profile_routes.follow_user, sub=SUB_A), headers=_bearer(TOKEN_A))

    assert response.status_code == 400


def test_follow_is_204_and_logs_action_followed_every_call():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    client, _repo, _profile_repo, _follow_repo, _t, _s, audit = _build(repository=repository)
    follow_path = _path(client, profile_routes.follow_user, sub=SUB_A)

    first = client.post(follow_path, headers=_bearer(TOKEN_B))
    second = client.post(follow_path, headers=_bearer(TOKEN_B))

    assert first.status_code == 204
    assert second.status_code == 204
    assert audit.entries == [(SUB_B, ACTION_FOLLOWED, SUB_A), (SUB_B, ACTION_FOLLOWED, SUB_A)]


def test_unfollow_is_always_204_but_only_logs_when_a_row_was_removed():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    client, *_rest, audit = _build(repository=repository)
    follow_path = _path(client, profile_routes.follow_user, sub=SUB_A)

    not_following_yet = client.delete(follow_path, headers=_bearer(TOKEN_B))
    assert not_following_yet.status_code == 204
    assert audit.entries == []

    client.post(follow_path, headers=_bearer(TOKEN_B))
    removed = client.delete(follow_path, headers=_bearer(TOKEN_B))
    assert removed.status_code == 204
    assert audit.entries == [(SUB_B, ACTION_FOLLOWED, SUB_A), (SUB_B, ACTION_UNFOLLOWED, SUB_A)]


def test_followers_unknown_sub_is_404():
    client, *_ = _build()

    response = client.get(_path(client, profile_routes.get_followers, sub=new_identity_sub()), headers=_bearer(TOKEN_A))

    assert response.status_code == 404


def test_followers_not_gated_by_is_public():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _HIDDEN
    client, *_ = _build(repository=repository, profile_repository=profile_repository)
    client.post(_path(client, profile_routes.follow_user, sub=SUB_A), headers=_bearer(TOKEN_B))

    response = client.get(_path(client, profile_routes.get_followers, sub=SUB_A), headers=_bearer(TOKEN_B))

    assert response.status_code == 200
    body = FollowListResponse.model_validate(response.json())
    assert body.total == 1
    assert body.entries[0].sub == SUB_B


def test_followers_pagination():
    repository = FakeRepository()
    followers = [new_identity_sub() for _ in range(3)]
    _seed_users(repository, SUB_A, *followers)
    follow_repository = FakeFollowRepository()
    client, *_ = _build(repository=repository, follow_repository=follow_repository)

    async def _seed():
        for follower in followers:
            await follow_repository.follow(follower, SUB_A)

    asyncio.run(_seed())

    response = client.get(
        _path(client, profile_routes.get_followers, sub=SUB_A),
        params={LIMIT_PARAM: 2, OFFSET_PARAM: 1},
        headers=_bearer(TOKEN_B),
    )

    assert response.status_code == 200
    body = FollowListResponse.model_validate(response.json())
    assert body.total == 3
    assert [entry.sub for entry in body.entries] == [followers[1], followers[0]]


def test_following_not_gated_by_is_public_and_unknown_sub_is_404():
    client, *_ = _build()

    response = client.get(_path(client, profile_routes.get_following, sub=new_identity_sub()), headers=_bearer(TOKEN_A))

    assert response.status_code == 404


def test_library_403_when_private():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(show_library=True)
    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    response = client.get(_path(client, profile_routes.get_user_library, sub=SUB_A), headers=_bearer(TOKEN_B))

    assert response.status_code == 403


def test_library_403_when_public_but_show_library_false():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(is_public=True)
    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    response = client.get(_path(client, profile_routes.get_user_library, sub=SUB_A), headers=_bearer(TOKEN_B))

    assert response.status_code == 403


def test_library_200_with_data_when_public_and_show_library_true():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(is_public=True, show_library=True)
    game = FakeLibraryGameView(
        rawg_enriched=True,
        opencritic_enriched=False,
        psn_enriched=True,
        cover_image_url=new_cover_image_url(),
        platforms=(PS5, PS3),
    )
    library_repository = FakeLibraryRepository({SUB_A: [game]})
    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, library_repository=library_repository
    )

    response = client.get(_path(client, profile_routes.get_user_library, sub=SUB_A), headers=_bearer(TOKEN_B))

    assert response.status_code == 200
    assert ProfileLibraryPageResponse.model_validate(response.json()) == ProfileLibraryPageResponse(
        games=[
            ProfileLibraryGameResponse(
                game_id=game.game_id,
                title=game.title,
                genre=None,
                rawg_rating=None,
                opencritic_rating=None,
                psn_rating=None,
                psn_product_id=None,
                rawg_enriched=True,
                opencritic_enriched=False,
                psn_enriched=True,
                is_active=True,
                percent_completed=None,
                cover_image_url=game.cover_image_url,
                platforms=[PS5, PS3],
            )
        ],
        total=1,
    )


def test_library_200_for_owner_regardless_of_flags():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _HIDDEN
    library_repository = FakeLibraryRepository({SUB_A: [FakeLibraryGameView()]})
    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, library_repository=library_repository
    )

    response = client.get(_path(client, profile_routes.get_user_library, sub=SUB_A), headers=_bearer(TOKEN_A))

    assert response.status_code == 200
    body = ProfileLibraryPageResponse.model_validate(response.json())
    assert len(body.games) == 1
    assert body.total == 1


def test_library_genres_returns_distinct_genres_when_visible():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(is_public=True, show_library=True)
    later_genre, earlier_genre = token_from_second_half_of_alphabet(), token_from_first_half_of_alphabet()
    library_repository = FakeLibraryRepository(
        {SUB_A: [FakeLibraryGameView(genre=later_genre), FakeLibraryGameView(genre=earlier_genre)]}
    )
    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, library_repository=library_repository
    )

    response = client.get(_path(client, profile_routes.get_user_library_genres, sub=SUB_A), headers=_bearer(TOKEN_B))

    assert response.status_code == 200
    assert LibraryGenresResponse.model_validate(response.json()).genres == [earlier_genre, later_genre]


def test_library_genres_403_when_private():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(show_library=True)
    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    response = client.get(_path(client, profile_routes.get_user_library_genres, sub=SUB_A), headers=_bearer(TOKEN_B))

    assert response.status_code == 403


def test_collections_403_when_private():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(show_collections=True)
    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    response = client.get(_path(client, profile_routes.get_user_collections, sub=SUB_A), headers=_bearer(TOKEN_B))

    assert response.status_code == 403


def test_collections_403_when_public_but_show_collections_false():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(is_public=True)
    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    response = client.get(_path(client, profile_routes.get_user_collections, sub=SUB_A), headers=_bearer(TOKEN_B))

    assert response.status_code == 403


def test_collections_200_with_data_when_public_and_show_collections_true():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(is_public=True, show_collections=True)
    definition = _definition(
        kind=CAPACITY_FILL_KIND,
        console_id=new_console_id(),
        visibility=VISIBILITY_PUBLIC,
        item_count=new_positive_count(),
    )
    collections_repository = FakeCollectionsRepository({SUB_A: [definition]})
    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, collections_repository=collections_repository
    )

    response = client.get(_path(client, profile_routes.get_user_collections, sub=SUB_A), headers=_bearer(TOKEN_B))

    assert response.status_code == 200
    assert _DEFINITIONS.validate_python(response.json()) == [
        ProfileDefinitionResponse(
            definition_id=definition.definition_id,
            name=definition.name,
            kind=CAPACITY_FILL_KIND,
            console_id=definition.console_id,
            item_count=definition.item_count,
        )
    ]


def test_collections_hides_unlisted_and_private_collections_from_a_non_owner():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _settings(is_public=True, show_collections=True)
    public_def = _definition(visibility=VISIBILITY_PUBLIC)
    collections_repository = FakeCollectionsRepository(
        {SUB_A: [public_def, _definition(visibility=VISIBILITY_UNLISTED), _definition(visibility=VISIBILITY_PRIVATE)]}
    )
    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, collections_repository=collections_repository
    )

    response = client.get(_path(client, profile_routes.get_user_collections, sub=SUB_A), headers=_bearer(TOKEN_B))

    assert response.status_code == 200
    assert [d.definition_id for d in _DEFINITIONS.validate_python(response.json())] == [public_def.definition_id]


def test_collections_200_for_owner_regardless_of_flags():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = _HIDDEN
    collections_repository = FakeCollectionsRepository({SUB_A: [_definition(kind=CAPACITY_FILL_KIND)]})
    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, collections_repository=collections_repository
    )

    response = client.get(_path(client, profile_routes.get_user_collections, sub=SUB_A), headers=_bearer(TOKEN_A))

    assert response.status_code == 200
    assert len(_DEFINITIONS.validate_python(response.json())) == 1


def _definition(**overrides):
    return replace(
        CollectionDefinition(
            definition_id=new_definition_id(),
            identity_sub=SUB_A,
            name=new_game_title(),
            kind=FILTER_LIST_KIND,
            console_id=None,
            genre_filter=(),
            min_score=None,
            aaa_tier_filter=None,
            sort_order=None,
        ),
        **overrides,
    )


def _link(*, psn_account_id, harvest_trophies=False, harvest_identity=False):
    return LinkRecord(
        psn_account_id=psn_account_id,
        token_response_enc=b"enc",
        access_token_expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        refresh_token_expires_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        linked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_verified_at=None,
        harvest_trophies=harvest_trophies,
        harvest_identity=harvest_identity,
    )
