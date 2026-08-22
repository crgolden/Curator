"""Tests for /me/profile-settings, /users/{sub}/profile, /users/{sub}/follow, /users/{sub}/followers,
/users/{sub}/following, /users/{sub}/library, and /users/{sub}/collections.

Big enough to warrant its own file (unlike trophy/identity/enrichment-keys, which share test_routes.py).
Every collaborator is a hand-written fake -- no unittest.mock, matching the rest of this suite. The fake
TrophyClientFactory/SocialClient stand-ins return canned data keyed by the ``account_id`` argument they
were called with, so tests can assert B's request resolved A's ``account_id``, never B's own.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.collections.repository import CollectionDefinition
from curator.persistence.crypto import TokenCrypto
from curator.persistence.follow_repository import FollowEdge
from curator.persistence.profile_link_repository import ProfileLink, ProfileLinkSite
from curator.persistence.profile_repository import ProfileSettings
from curator.persistence.repository import LinkRecord
from curator.profile_routes import ProfileIdentityResponse
from curator.psn.errors import PsnAuthError
from curator.psn.models import TrophyCounts, TrophySummary
from test_routes import FakeAuditRepository, FakeRepository, FakeTokenValidator, _bearer, _claims, _make_settings

SUB_A = "sub-a"
SUB_B = "sub-b"


class FakeProfileRepository:
    def __init__(self) -> None:
        self.settings: dict[str, ProfileSettings] = {}
        self.upsert_calls: list[tuple] = []

    async def get_settings(self, sub: str) -> ProfileSettings:
        return self.settings.get(
            sub,
            ProfileSettings(
                is_public=False, show_library=False, show_collections=False, show_trophies=False, show_identity=False
            ),
        )

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
            raise PsnAuthError("boom")
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
            raise RuntimeError(f"No PSN link for user {sub!r}; cannot fetch trophies.")
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
            raise PsnAuthError("boom")
        return self._online_ids.get(account_id)


class FakeProfileSocialClientFactory:
    def __init__(self) -> None:
        self.linked: dict[str, FakeProfileSocialClient] = {}
        self.calls: list[str] = []

    async def __call__(self, sub: str):
        self.calls.append(sub)
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(f"No PSN link for user {sub!r}; cannot fetch identity.")
        return client


class FakeLibraryRepository:
    def __init__(self, games_by_sub=None) -> None:
        self._games_by_sub = games_by_sub or {}

    async def count_entries(self, identity_sub: str) -> int:
        return len(self._games_by_sub.get(identity_sub, []))

    async def list_entries_with_enrichment(
        self, identity_sub: str, *, search=None, category=None, sort="title", sort_dir="asc", limit=20, offset=0
    ):
        games = self._games_by_sub.get(identity_sub, [])
        return games[offset : offset + limit], len(games)

    async def list_categories(self, identity_sub: str):
        games = self._games_by_sub.get(identity_sub, [])
        return sorted({g.category for g in games if g.category is not None})


class FakeLibraryGameView:
    def __init__(
        self,
        game_id,
        title,
        category=None,
        rawg_rating=None,
        opencritic_rating=None,
        psn_rating=None,
        psn_product_id=None,
        rawg_enriched=False,
        opencritic_enriched=False,
        is_active=True,
        percent_completed=None,
        cover_image_url=None,
        platforms=(),
    ) -> None:
        self.game_id = game_id
        self.title = title
        self.category = category
        self.rawg_rating = rawg_rating
        self.opencritic_rating = opencritic_rating
        self.psn_rating = psn_rating
        self.psn_product_id = psn_product_id
        self.rawg_enriched = rawg_enriched
        self.opencritic_enriched = opencritic_enriched
        self.is_active = is_active
        self.percent_completed = percent_completed
        self.cover_image_url = cover_image_url
        self.platforms = platforms


class FakeProfileLinkRepository:
    def __init__(self, links_by_sub=None, sites=None) -> None:
        self._links_by_sub = links_by_sub or {}
        self._sites = sites or [
            ProfileLinkSite(
                site_key="psnprofiles",
                display_name="PSNProfiles",
                url_template="https://psnprofiles.com/{handle}",
                sort_order=1,
            )
        ]

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
            url=site.url_template.replace("{handle}", handle),
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
            return len([d for d in definitions if d.visibility == "public"])
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
    validator.register("token-a", _claims(sub=SUB_A, email="a@example.com"))
    validator.register("token-b", _claims(sub=SUB_B, email="b@example.com"))

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


def test_get_my_profile_settings_never_404s_with_no_row():
    client, *_ = _build()
    response = client.get("/me/profile-settings", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == {
        "is_public": False,
        "show_library": False,
        "show_collections": False,
        "show_trophies": False,
        "show_identity": False,
    }


def test_put_profile_settings_never_leaks_across_users():
    client, _repo, profile_repository, *_ = _build()

    client.put(
        "/me/profile-settings",
        json={
            "is_public": True,
            "show_library": True,
            "show_collections": False,
            "show_trophies": False,
            "show_identity": False,
        },
        headers=_bearer("token-a"),
    )
    client.put(
        "/me/profile-settings",
        json={
            "is_public": False,
            "show_library": False,
            "show_collections": True,
            "show_trophies": True,
            "show_identity": True,
        },
        headers=_bearer("token-b"),
    )

    a_settings = client.get("/me/profile-settings", headers=_bearer("token-a")).json()
    b_settings = client.get("/me/profile-settings", headers=_bearer("token-b")).json()

    assert a_settings == {
        "is_public": True,
        "show_library": True,
        "show_collections": False,
        "show_trophies": False,
        "show_identity": False,
    }
    assert b_settings == {
        "is_public": False,
        "show_library": False,
        "show_collections": True,
        "show_trophies": True,
        "show_identity": True,
    }
    assert {call[0] for call in profile_repository.upsert_calls} == {SUB_A, SUB_B}


def test_get_profile_unknown_sub_is_404():
    client, *_ = _build()
    response = client.get("/users/nobody/profile", headers=_bearer("token-a"))
    assert response.status_code == 404


def test_owner_viewing_own_private_profile_sees_everything():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=True, show_collections=True, show_trophies=True, show_identity=True
    )
    repository.links[SUB_A] = _link(psn_account_id="acct-a", harvest_trophies=True, harvest_identity=True)

    trophy_summary = TrophySummary(level=42, progress=50, tier=3, earned=TrophyCounts(1, 2, 3, 4), account_id="acct-a")
    trophy_client = FakeProfileTrophyClient({"acct-a": trophy_summary})
    trophy_factory = FakeProfileTrophyClientFactory()
    trophy_factory.linked[SUB_A] = trophy_client

    social_client = FakeProfileSocialClient({"acct-a": "OwnerOnlineId"})
    social_factory = FakeProfileSocialClientFactory()
    social_factory.linked[SUB_A] = social_client

    client, *_ = _build(
        repository=repository,
        profile_repository=profile_repository,
        trophy_client_factory=trophy_factory,
        social_client_factory=social_factory,
    )

    response = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-a"))
    body = response.json()

    assert response.status_code == 200
    assert body["viewer_is_owner"] is True
    assert body["psn_account_id"] == "acct-a"
    assert body["library_visible"] is True
    assert body["collections_visible"] is True
    assert body["trophies"] == {"level": 42, "tier": 3, "earned": {"bronze": 1, "silver": 2, "gold": 3, "platinum": 4}}
    assert body["identity"] == {"online_id": "OwnerOnlineId"}
    assert trophy_client.calls == ["acct-a"]
    assert social_client.calls == ["acct-a"]


def test_non_owner_viewing_a_private_profile_sees_only_counts_and_follow_status():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=True, show_collections=True, show_trophies=True, show_identity=True
    )
    repository.links[SUB_A] = _link(psn_account_id="acct-a", harvest_trophies=True, harvest_identity=True)
    follow_repository = FakeFollowRepository()

    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, follow_repository=follow_repository
    )

    response = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-b"))
    body = response.json()

    assert response.status_code == 200
    assert body["sub"] == SUB_A
    assert body["is_public"] is False
    assert body["viewer_is_owner"] is False
    assert body["viewer_is_following"] is False
    assert body["follower_count"] == 0
    assert body["following_count"] == 0
    assert body["psn_account_id"] is None
    assert body["library_visible"] is False
    assert body["collections_visible"] is False
    assert body["trophies"] is None
    assert body["identity"] is None


def test_profile_body_declares_every_field_it_returns():
    """Pins the response's whole key set, so a new field cannot be added without a deliberate choice.

    Every other assertion in this module checks keys individually, which means an ungated field added to
    ``PublicProfileResponse`` would sail past all of them -- including the private-profile test whose
    name claims it sees "only counts and follow status". This is the test that fails instead.
    """
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    client, *_ = _build(repository=repository)

    body = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-b")).json()

    assert set(body) == {
        "sub",
        "psn_account_id",
        "is_public",
        "viewer_is_owner",
        "viewer_is_following",
        "follower_count",
        "following_count",
        "library_visible",
        "collections_visible",
        "trophies",
        "identity",
        "created_at",
        "library_count",
        "collections_count",
        "trophies_hidden_by_owner_setting",
        "profile_links",
    }


def test_private_profile_withholds_both_counts_from_a_non_owner():
    """The information-leak test. Paired with a non-empty dataset on purpose.

    ``GET /users/{sub}/library`` already 403s here, so a count in the profile body would hand the viewer
    the most useful fact behind that 403. Seeding real rows is what makes the ``None`` provably
    suppression rather than an empty library -- see the owner-side test below, which reads the same
    fixtures and gets numbers.
    """
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=True, show_collections=True, show_trophies=False, show_identity=False
    )
    library_repository = FakeLibraryRepository({SUB_A: [FakeLibraryGameView("g1", "Elden Ring")]})
    collections_repository = FakeCollectionsRepository({SUB_A: [_definition("d1", visibility="public")]})

    client, *_ = _build(
        repository=repository,
        profile_repository=profile_repository,
        library_repository=library_repository,
        collections_repository=collections_repository,
    )

    body = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-b")).json()

    assert body["library_visible"] is False
    assert body["collections_visible"] is False
    assert body["library_count"] is None
    assert body["collections_count"] is None


def test_owner_sees_real_counts_from_the_same_fixtures_the_viewer_is_denied():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=False, show_collections=False, show_trophies=False, show_identity=False
    )
    library_repository = FakeLibraryRepository({SUB_A: [FakeLibraryGameView("g1", "Elden Ring")]})
    collections_repository = FakeCollectionsRepository({SUB_A: [_definition("d1", visibility="public")]})

    client, *_ = _build(
        repository=repository,
        profile_repository=profile_repository,
        library_repository=library_repository,
        collections_repository=collections_repository,
    )

    body = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-a")).json()

    assert body["library_count"] == 1
    assert body["collections_count"] == 1


def test_collections_count_for_a_non_owner_excludes_private_and_unlisted():
    """Must match what ``GET /users/{sub}/collections`` lists, which is ``visibility == "public"``.

    Counting with ``!= 'private'`` -- the predicate the *share-slug* lookup uses -- would include the
    unlisted one and visibly contradict the list beneath it.
    """
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=True, show_library=False, show_collections=True, show_trophies=False, show_identity=False
    )
    collections_repository = FakeCollectionsRepository(
        {
            SUB_A: [
                _definition("d-public", visibility="public"),
                _definition("d-unlisted", visibility="unlisted"),
                _definition("d-private", visibility="private"),
            ]
        }
    )

    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, collections_repository=collections_repository
    )

    viewer_body = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-b")).json()
    owner_body = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-a")).json()

    assert viewer_body["collections_count"] == 1
    assert owner_body["collections_count"] == 3


def test_library_count_includes_inactive_entries():
    """Matches ``list_entries_with_enrichment``, which applies no ``is_active`` filter either.

    Filtering here would make the profile tile disagree with the total the library page itself reports.
    """
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=True, show_collections=False, show_trophies=False, show_identity=False
    )
    library_repository = FakeLibraryRepository(
        {
            SUB_A: [
                FakeLibraryGameView("g1", "Active game", is_active=True),
                FakeLibraryGameView("g2", "Delisted game", is_active=False),
            ]
        }
    )

    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, library_repository=library_repository
    )

    body = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-a")).json()

    assert body["library_count"] == 2


def test_created_at_is_returned_even_to_a_viewer_of_a_private_profile():
    """Ungated on purpose: it describes the account, not its content, and the existing 404-vs-200 on an
    unknown sub already discloses that the account exists."""
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=False, show_collections=False, show_trophies=False, show_identity=False
    )

    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    body = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-b")).json()

    assert body["created_at"] == "2026-01-02T03:04:05+00:00"


def test_trophies_hidden_by_owner_setting_is_owner_only():
    """A viewer must never learn *why* another user's trophy section is absent -- that would leak the
    target's own settings, which is exactly what this module's gating exists to prevent."""
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=True, show_library=False, show_collections=False, show_trophies=False, show_identity=False
    )
    repository.links[SUB_A] = _link(psn_account_id="acct-a", harvest_trophies=False)

    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    owner_body = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-a")).json()
    viewer_body = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-b")).json()

    assert owner_body["trophies"] is None
    assert owner_body["trophies_hidden_by_owner_setting"] is True
    assert viewer_body["trophies_hidden_by_owner_setting"] is False


def test_trophies_hidden_flag_is_false_when_the_owner_has_no_psn_link_at_all():
    """An unlinked owner has nothing to switch on, so pointing them at a toggle would be wrong advice."""
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=False, show_collections=False, show_trophies=False, show_identity=False
    )

    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    body = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-a")).json()

    assert body["trophies_hidden_by_owner_setting"] is False


def test_profile_links_are_withheld_from_a_viewer_of_a_private_profile():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=False, show_collections=False, show_trophies=False, show_identity=False
    )
    link_repository = FakeProfileLinkRepository(
        {
            SUB_A: [
                ProfileLink(
                    site_key="psnprofiles",
                    display_name="PSNProfiles",
                    handle="deeprog",
                    url="https://psnprofiles.com/deeprog",
                )
            ]
        }
    )

    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, profile_link_repository=link_repository
    )

    viewer_body = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-b")).json()
    owner_body = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-a")).json()

    assert viewer_body["profile_links"] == []
    assert owner_body["profile_links"][0]["url"] == "https://psnprofiles.com/deeprog"


def test_setting_a_profile_link_builds_the_url_from_the_site_template():
    """The stored value is a handle; Curator builds the URL. A caller never supplies one, which is what
    keeps an attacker-controlled string out of the ``href`` a different user clicks."""
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    client, *_ = _build(repository=repository)

    response = client.put("/me/profile-links/psnprofiles", json={"handle": "deeprog"}, headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == {
        "site_key": "psnprofiles",
        "display_name": "PSNProfiles",
        "handle": "deeprog",
        "url": "https://psnprofiles.com/deeprog",
    }


def test_setting_a_profile_link_rejects_a_site_outside_the_allowlist():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    client, *_ = _build(repository=repository)

    response = client.put("/me/profile-links/evil-site", json={"handle": "deeprog"}, headers=_bearer("token-a"))

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
    """The handle is the only user-controlled part of the rendered URL, so its charset is the whole
    boundary. Rejected at the route so the caller sees a 400 rather than a CHECK violation as a 500."""
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    client, *_ = _build(repository=repository)

    response = client.put("/me/profile-links/psnprofiles", json={"handle": handle}, headers=_bearer("token-a"))

    assert response.status_code == 400


def test_deleting_a_profile_link_is_idempotent():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    client, *_ = _build(repository=repository)

    client.put("/me/profile-links/psnprofiles", json={"handle": "deeprog"}, headers=_bearer("token-a"))

    first = client.delete("/me/profile-links/psnprofiles", headers=_bearer("token-a"))
    second = client.delete("/me/profile-links/psnprofiles", headers=_bearer("token-a"))

    assert first.status_code == 204
    assert second.status_code == 204
    assert client.get("/me/profile-links", headers=_bearer("token-a")).json() == []


def test_show_trophies_true_but_harvest_trophies_false_is_none():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=True, show_library=False, show_collections=False, show_trophies=True, show_identity=False
    )
    repository.links[SUB_A] = _link(psn_account_id="acct-a", harvest_trophies=False)

    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    response = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-b"))
    assert response.status_code == 200
    assert response.json()["trophies"] is None


def test_show_and_harvest_trophies_true_but_viewer_has_no_psn_link_is_none_and_still_200():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=True, show_library=False, show_collections=False, show_trophies=True, show_identity=False
    )
    repository.links[SUB_A] = _link(psn_account_id="acct-a", harvest_trophies=True)
    trophy_factory = FakeProfileTrophyClientFactory()

    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, trophy_client_factory=trophy_factory
    )

    response = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-b"))
    assert response.status_code == 200
    assert response.json()["trophies"] is None


def test_cross_user_trophy_lookup_uses_the_targets_account_id_via_the_viewers_own_client():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=True, show_library=False, show_collections=False, show_trophies=True, show_identity=True
    )
    repository.links[SUB_A] = _link(psn_account_id="acct-a", harvest_trophies=True, harvest_identity=True)

    trophy_summary = TrophySummary(level=7, progress=0, tier=1, earned=TrophyCounts(), account_id="acct-a")
    trophy_client = FakeProfileTrophyClient({"acct-a": trophy_summary})
    trophy_factory = FakeProfileTrophyClientFactory()
    trophy_factory.linked[SUB_B] = trophy_client

    social_client = FakeProfileSocialClient({"acct-a": "TargetOnlineId"})
    social_factory = FakeProfileSocialClientFactory()
    social_factory.linked[SUB_B] = social_client

    client, *_ = _build(
        repository=repository,
        profile_repository=profile_repository,
        trophy_client_factory=trophy_factory,
        social_client_factory=social_factory,
    )

    response = client.get(f"/users/{SUB_A}/profile", headers=_bearer("token-b"))
    body = response.json()

    assert response.status_code == 200
    assert body["trophies"]["level"] == 7
    assert body["identity"] == {"online_id": "TargetOnlineId"}
    assert trophy_factory.calls == [SUB_B]
    assert trophy_client.calls == ["acct-a"]
    assert social_factory.calls == [SUB_B]
    assert social_client.calls == ["acct-a"]


def test_profile_identity_response_has_no_region_field_at_all():
    assert "region" not in ProfileIdentityResponse.model_fields


def test_follow_unknown_sub_is_404():
    client, *_ = _build()
    response = client.post("/users/nobody/follow", headers=_bearer("token-a"))
    assert response.status_code == 404


def test_follow_self_is_400():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    client, *_ = _build(repository=repository)

    response = client.post(f"/users/{SUB_A}/follow", headers=_bearer("token-a"))
    assert response.status_code == 400


def test_follow_is_204_and_logs_action_followed_every_call():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    client, _repo, _profile_repo, _follow_repo, _t, _s, audit = _build(repository=repository)

    first = client.post(f"/users/{SUB_A}/follow", headers=_bearer("token-b"))
    second = client.post(f"/users/{SUB_A}/follow", headers=_bearer("token-b"))

    assert first.status_code == 204
    assert second.status_code == 204
    assert audit.entries == [(SUB_B, "followed", SUB_A), (SUB_B, "followed", SUB_A)]


def test_unfollow_is_always_204_but_only_logs_when_a_row_was_removed():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    client, *_rest, audit = _build(repository=repository)

    not_following_yet = client.delete(f"/users/{SUB_A}/follow", headers=_bearer("token-b"))
    assert not_following_yet.status_code == 204
    assert audit.entries == []

    client.post(f"/users/{SUB_A}/follow", headers=_bearer("token-b"))
    removed = client.delete(f"/users/{SUB_A}/follow", headers=_bearer("token-b"))
    assert removed.status_code == 204
    assert audit.entries == [(SUB_B, "followed", SUB_A), (SUB_B, "unfollowed", SUB_A)]


def test_followers_unknown_sub_is_404():
    client, *_ = _build()
    response = client.get("/users/nobody/followers", headers=_bearer("token-a"))
    assert response.status_code == 404


def test_followers_not_gated_by_is_public():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=False, show_collections=False, show_trophies=False, show_identity=False
    )
    client, *_ = _build(repository=repository, profile_repository=profile_repository)
    client.post(f"/users/{SUB_A}/follow", headers=_bearer("token-b"))

    response = client.get(f"/users/{SUB_A}/followers", headers=_bearer("token-b"))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["entries"][0]["sub"] == SUB_B


def test_followers_pagination():
    repository = FakeRepository()
    _seed_users(repository, "sub-a", "sub-b", "sub-c", "sub-d")
    follow_repository = FakeFollowRepository()
    client, *_ = _build(repository=repository, follow_repository=follow_repository)

    async def _seed():
        await follow_repository.follow("sub-b", "sub-a")
        await follow_repository.follow("sub-c", "sub-a")
        await follow_repository.follow("sub-d", "sub-a")

    asyncio.run(_seed())

    response = client.get("/users/sub-a/followers", params={"limit": 2, "offset": 1}, headers=_bearer("token-b"))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert len(body["entries"]) == 2


def test_following_not_gated_by_is_public_and_unknown_sub_is_404():
    client, *_ = _build()
    response = client.get("/users/nobody/following", headers=_bearer("token-a"))
    assert response.status_code == 404


def test_library_403_when_private():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=True, show_collections=False, show_trophies=False, show_identity=False
    )
    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    response = client.get(f"/users/{SUB_A}/library", headers=_bearer("token-b"))
    assert response.status_code == 403


def test_library_403_when_public_but_show_library_false():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=True, show_library=False, show_collections=False, show_trophies=False, show_identity=False
    )
    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    response = client.get(f"/users/{SUB_A}/library", headers=_bearer("token-b"))
    assert response.status_code == 403


def test_library_200_with_data_when_public_and_show_library_true():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=True, show_library=True, show_collections=False, show_trophies=False, show_identity=False
    )
    library_repository = FakeLibraryRepository(
        {
            SUB_A: [
                FakeLibraryGameView(
                    "game-1",
                    "Elden Ring",
                    rawg_enriched=True,
                    opencritic_enriched=False,
                    cover_image_url="https://cdn.example/elden-ring.jpg",
                    platforms=("PS5", "PS3"),
                )
            ]
        }
    )
    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, library_repository=library_repository
    )

    response = client.get(f"/users/{SUB_A}/library", headers=_bearer("token-b"))
    assert response.status_code == 200
    assert response.json() == {
        "games": [
            {
                "game_id": "game-1",
                "title": "Elden Ring",
                "category": None,
                "rawg_rating": None,
                "opencritic_rating": None,
                "psn_rating": None,
                "psn_product_id": None,
                "rawg_enriched": True,
                "opencritic_enriched": False,
                "is_active": True,
                "percent_completed": None,
                "cover_image_url": "https://cdn.example/elden-ring.jpg",
                "platforms": ["PS5", "PS3"],
            }
        ],
        "total": 1,
    }


def test_library_200_for_owner_regardless_of_flags():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=False, show_collections=False, show_trophies=False, show_identity=False
    )
    library_repository = FakeLibraryRepository({SUB_A: [FakeLibraryGameView("game-1", "Elden Ring")]})
    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, library_repository=library_repository
    )

    response = client.get(f"/users/{SUB_A}/library", headers=_bearer("token-a"))
    assert response.status_code == 200
    body = response.json()
    assert len(body["games"]) == 1
    assert body["total"] == 1


def test_library_categories_returns_distinct_categories_when_visible():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=True, show_library=True, show_collections=False, show_trophies=False, show_identity=False
    )
    library_repository = FakeLibraryRepository(
        {SUB_A: [FakeLibraryGameView("g1", "A", category="RPG"), FakeLibraryGameView("g2", "B", category="Puzzle")]}
    )
    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, library_repository=library_repository
    )

    response = client.get(f"/users/{SUB_A}/library/categories", headers=_bearer("token-b"))
    assert response.status_code == 200
    assert response.json() == {"categories": ["Puzzle", "RPG"]}


def test_library_categories_403_when_private():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=True, show_collections=False, show_trophies=False, show_identity=False
    )
    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    response = client.get(f"/users/{SUB_A}/library/categories", headers=_bearer("token-b"))
    assert response.status_code == 403


def test_collections_403_when_private():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=False, show_collections=True, show_trophies=False, show_identity=False
    )
    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    response = client.get(f"/users/{SUB_A}/collections", headers=_bearer("token-b"))
    assert response.status_code == 403


def test_collections_403_when_public_but_show_collections_false():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=True, show_library=False, show_collections=False, show_trophies=False, show_identity=False
    )
    client, *_ = _build(repository=repository, profile_repository=profile_repository)

    response = client.get(f"/users/{SUB_A}/collections", headers=_bearer("token-b"))
    assert response.status_code == 403


def test_collections_200_with_data_when_public_and_show_collections_true():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=True, show_library=False, show_collections=True, show_trophies=False, show_identity=False
    )
    definition = CollectionDefinition(
        definition_id="def-1",
        identity_sub=SUB_A,
        name="PS5 Fill",
        kind="capacity_fill",
        console_id="console-1",
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
        visibility="public",
        item_count=5,
    )
    collections_repository = FakeCollectionsRepository({SUB_A: [definition]})
    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, collections_repository=collections_repository
    )

    response = client.get(f"/users/{SUB_A}/collections", headers=_bearer("token-b"))
    assert response.status_code == 200
    assert response.json() == [
        {
            "definition_id": "def-1",
            "name": "PS5 Fill",
            "kind": "capacity_fill",
            "console_id": "console-1",
            "item_count": 5,
        }
    ]


def test_collections_hides_unlisted_and_private_collections_from_a_non_owner():
    repository = FakeRepository()
    _seed_users(repository, SUB_A, SUB_B)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=True, show_library=False, show_collections=True, show_trophies=False, show_identity=False
    )
    public_def = CollectionDefinition(
        definition_id="def-public",
        identity_sub=SUB_A,
        name="Public one",
        kind="filter_list",
        console_id=None,
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
        visibility="public",
    )
    unlisted_def = CollectionDefinition(
        definition_id="def-unlisted",
        identity_sub=SUB_A,
        name="Unlisted one",
        kind="filter_list",
        console_id=None,
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
        visibility="unlisted",
    )
    private_def = CollectionDefinition(
        definition_id="def-private",
        identity_sub=SUB_A,
        name="Private one",
        kind="filter_list",
        console_id=None,
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
        visibility="private",
    )
    collections_repository = FakeCollectionsRepository({SUB_A: [public_def, unlisted_def, private_def]})
    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, collections_repository=collections_repository
    )

    response = client.get(f"/users/{SUB_A}/collections", headers=_bearer("token-b"))

    assert response.status_code == 200
    assert [d["definition_id"] for d in response.json()] == ["def-public"]


def test_collections_200_for_owner_regardless_of_flags():
    repository = FakeRepository()
    _seed_users(repository, SUB_A)
    profile_repository = FakeProfileRepository()
    profile_repository.settings[SUB_A] = ProfileSettings(
        is_public=False, show_library=False, show_collections=False, show_trophies=False, show_identity=False
    )
    definition = CollectionDefinition(
        definition_id="def-1",
        identity_sub=SUB_A,
        name="PS5 Fill",
        kind="capacity_fill",
        console_id=None,
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
    )
    collections_repository = FakeCollectionsRepository({SUB_A: [definition]})
    client, *_ = _build(
        repository=repository, profile_repository=profile_repository, collections_repository=collections_repository
    )

    response = client.get(f"/users/{SUB_A}/collections", headers=_bearer("token-a"))
    assert response.status_code == 200
    assert len(response.json()) == 1


def _definition(definition_id, *, visibility="private", identity_sub=None):
    return CollectionDefinition(
        definition_id=definition_id,
        identity_sub=identity_sub or SUB_A,
        name=definition_id,
        kind="filter_list",
        console_id=None,
        genre_filter=(),
        min_score=None,
        aaa_tier_filter=None,
        sort_order=None,
        visibility=visibility,
        item_count=0,
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
