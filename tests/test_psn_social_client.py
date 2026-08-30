"""Tests for SocialClient, using a hand-written fake session (no network, no credentials).

Ported from ``psnpy``'s ``test_social.py``/``test_capabilities.py``.
"""

from __future__ import annotations

import json
import uuid

import pytest

from curator.psn.models import AccountDevice, Friendship, PlayerSearchResult, Profile, ProfileShareLink, SocialUser
from curator.psn.social_client import (
    ADD_ONS_DOMAIN,
    FULL_GAMES_DOMAIN,
    GAME_SEARCH_CONTEXT,
    MAX_GAME_SEARCH_PAGES,
    SOCIAL_SEARCH_CONTEXT,
    SocialClient,
)


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *, own_account_id="123", responses=None, online_ids=None, devices_body=None):
        self._own_account_id = own_account_id
        self._responses = dict(responses or {})
        self._online_ids = dict(online_ids or {})
        self._devices_body = devices_body
        self.get_calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, headers=None):
        self.get_calls.append((url, params or {}))
        if "devices/accounts/me" in url:
            if self._devices_body is not None and "includeFields" in (params or {}):
                return FakeResponse(self._devices_body)
            return FakeResponse({"accountId": self._own_account_id})
        if url.endswith("/profiles"):
            account_id = url.rsplit("/", 2)[-2]
            return FakeResponse({"onlineId": self._online_ids.get(account_id, f"user-{account_id}")})
        operation_name = (params or {}).get("operationName")
        if operation_name:
            body = self._responses.get(operation_name, {})
            if isinstance(body, list):
                return FakeResponse(body.pop(0) if body else {})
            return FakeResponse(body)
        for key, body in self._responses.items():
            if key in url:
                return FakeResponse(body)
        return FakeResponse({})

    async def run_with_reauth(self, operation):
        return await operation()


async def test_friends_resolves_online_ids():
    client = SocialClient(
        FakeSession(responses={"friends": {"friends": ["1", "2"]}}, online_ids={"1": "Alice", "2": "Bob"})
    )

    friends = await client.friends()

    assert friends == [SocialUser(account_id="1", online_id="Alice"), SocialUser(account_id="2", online_id="Bob")]


async def test_blocked_resolves_online_ids():
    client = SocialClient(FakeSession(responses={"me/blocks": {"blockList": ["9"]}}, online_ids={"9": "Baddie"}))

    blocked = await client.blocked()

    assert blocked == [SocialUser(account_id="9", online_id="Baddie")]


async def test_available_to_play_resolves_online_ids():
    body = {"settings": [{"accountId": "5"}]}
    client = SocialClient(FakeSession(responses={"availableToPlay": body}, online_ids={"5": "Casey"}))

    result = await client.available_to_play()

    assert result == [SocialUser(account_id="5", online_id="Casey")]


async def test_friend_requests_resolves_online_ids():
    body = {"receivedRequests": [{"accountId": "7"}]}
    client = SocialClient(FakeSession(responses={"receivedRequests": body}, online_ids={"7": "Dana"}))

    result = await client.friend_requests()

    assert result == [SocialUser(account_id="7", online_id="Dana")]


async def test_friendship_maps_fields():
    body = {"friendRelation": "friend", "friendsCount": 10, "mutualFriendsCount": 3}
    client = SocialClient(FakeSession(responses={"summary": body}))

    result = await client.friendship(account_id="42")

    assert result == Friendship(
        relation="friend", personal_detail_sharing=None, friends_count=10, mutual_friends_count=3
    )


async def test_friendship_requires_a_target():
    client = SocialClient(FakeSession())

    with pytest.raises(ValueError, match="requires a target"):
        await client.friendship()


async def test_profile_never_hydrates_personal_detail():
    body = {"profile": {"aboutMe": "Hello", "avatarUrls": [{"avatarUrl": "a.png"}], "isOfficiallyVerified": True}}
    client = SocialClient(FakeSession(responses={"profile2": body}, online_ids={"123": "Me"}))

    profile = await client.profile()

    assert profile == Profile(
        about_me="Hello",
        avatars=("a.png",),
        languages=(),
        is_officially_verified=True,
        personal_detail=None,
    )


async def test_is_blocked_true_and_false():
    client = SocialClient(FakeSession(responses={"me/blocks": {"blockList": ["7"]}}))

    assert await client.is_blocked(account_id="7") is True
    assert await client.is_blocked(account_id="8") is False


async def test_is_blocked_requires_a_target():
    client = SocialClient(FakeSession())

    with pytest.raises(ValueError, match="requires a target"):
        await client.is_blocked()


async def test_devices_maps_fields():
    body = {
        "accountDevices": [
            {
                "deviceId": "d1",
                "deviceType": "PS5",
                "deviceName": "My PS5",
                "activationType": "PRIMARY",
                "activationDate": "2020-01-01",
            }
        ]
    }
    client = SocialClient(FakeSession(devices_body=body))

    devices = await client.devices()

    assert devices == [
        AccountDevice(
            device_id="d1",
            device_type="PS5",
            device_name="My PS5",
            activation_type="PRIMARY",
            activation_date="2020-01-01",
            deactivation_date=None,
        )
    ]


async def test_share_link_maps_fields():
    body = {"shareUrl": "https://psn/share", "shareImageUrl": "qr.png", "shareImageUrlDestination": "https://psn/dest"}
    client = SocialClient(FakeSession(responses={"share/profile": body}))

    result = await client.share_link()

    assert result == ProfileShareLink(
        share_url="https://psn/share",
        share_image_url="qr.png",
        share_image_url_destination="https://psn/dest",
    )


async def test_universal_search_players_maps_first_page():
    context_response = {
        "data": {
            "universalContextSearch": {
                "results": [
                    {
                        "searchResults": [
                            {"result": {"accountId": "1", "onlineId": "Alice", "isPsPlus": True}},
                        ],
                        "next": "",
                    }
                ]
            }
        }
    }
    client = SocialClient(FakeSession(responses={"metGetContextSearchResults": context_response}))

    results = await client.universal_search_players("alice")

    assert results == [
        PlayerSearchResult(account_id="1", online_id="Alice", avatar_url=None, is_ps_plus=True, relationship=None)
    ]


def _domain(domain, search_results, *, next_cursor=""):
    """One entry of ``data.universalContextSearch.results``, in the gateway's own shape."""
    return {"domain": domain, "searchResults": list(search_results), "next": next_cursor, "totalResultCount": 0}


def _context_search(domains):
    return {"data": {"universalContextSearch": {"results": list(domains)}}}


def _game_hit(result, *, item_id=None):
    """One ``searchResults`` entry: a wrapper carrying its own id plus the ``result`` node under it."""
    return {"__typename": "SearchResultItem", "id": item_id or uuid.uuid4().hex, "result": result}


def _concept(store_id, name):
    """A ``Concept`` result node in the shape ``psnawp``'s ``games_search_datatypes`` declares."""
    return {
        "__typename": "Concept",
        "id": store_id,
        "name": name,
        "invariantName": name,
        "platforms": ["PS5"],
        "media": [{"role": "GAMEHUB_COVER_ART", "type": "IMAGE", "url": f"https://cdn.invalid/{store_id}.jpg"}],
        "localizedStoreDisplayClassification": "Full Game",
    }


async def test_universal_search_games_reads_the_title_from_the_result_node_not_the_search_item():
    """The search item carries an ``id`` of its own beside the ``result`` node it wraps. Reading the
    wrapper yields an id nothing joins on and no title at all, which is how an earlier pass got ids right
    and ``None`` for every name."""
    expected_store_id = uuid.uuid4().hex
    expected_title = f"Ghost of {uuid.uuid4().hex}"
    wrapper_id = uuid.uuid4().hex
    hit = _game_hit(_concept(expected_store_id, expected_title), item_id=wrapper_id)
    client = SocialClient(
        FakeSession(responses={"metGetContextSearchResults": _context_search([_domain(FULL_GAMES_DOMAIN, [hit])])})
    )

    results = await client.universal_search_games(expected_title)

    assert [(result.id, result.name) for result in results] == [(expected_store_id, expected_title)]
    assert results[0].id != wrapper_id


async def test_universal_search_games_selects_the_container_by_its_domain_label_not_its_position():
    """PSNAWP indexes this list positionally (FULL_GAMES = 0). Nothing in the payload promises that order,
    so the add-ons container arriving first must not be served as the full-games result."""
    add_on_title = f"Add-On {uuid.uuid4().hex}"
    full_game_title = f"Full Game {uuid.uuid4().hex}"
    add_on_hit = _game_hit(_concept(uuid.uuid4().hex, add_on_title))
    full_game_hit = _game_hit(_concept(uuid.uuid4().hex, full_game_title))
    client = SocialClient(
        FakeSession(
            responses={
                "metGetContextSearchResults": _context_search(
                    [_domain(ADD_ONS_DOMAIN, [add_on_hit]), _domain(FULL_GAMES_DOMAIN, [full_game_hit])]
                )
            }
        )
    )

    results = await client.universal_search_games(full_game_title)

    assert [result.name for result in results] == [full_game_title]


async def test_universal_search_games_reads_the_add_ons_domain_when_asked_for_it():
    add_on_title = f"Add-On {uuid.uuid4().hex}"
    add_on_hit = _game_hit(_concept(uuid.uuid4().hex, add_on_title))
    full_game_hit = _game_hit(_concept(uuid.uuid4().hex, f"Full Game {uuid.uuid4().hex}"))
    client = SocialClient(
        FakeSession(
            responses={
                "metGetContextSearchResults": _context_search(
                    [_domain(FULL_GAMES_DOMAIN, [full_game_hit]), _domain(ADD_ONS_DOMAIN, [add_on_hit])]
                )
            }
        )
    )

    results = await client.universal_search_games(add_on_title, domain=ADD_ONS_DOMAIN)

    assert [result.name for result in results] == [add_on_title]


async def test_universal_search_games_returns_nothing_when_psn_sends_no_container_for_that_domain():
    """Indexing the list positionally raises IndexError here; matching the label degrades to empty."""
    full_game_hit = _game_hit(_concept(uuid.uuid4().hex, f"Full Game {uuid.uuid4().hex}"))
    client = SocialClient(
        FakeSession(
            responses={"metGetContextSearchResults": _context_search([_domain(FULL_GAMES_DOMAIN, [full_game_hit])])}
        )
    )

    results = await client.universal_search_games("anything", domain=ADD_ONS_DOMAIN)

    assert results == []


def _domain_page(search_results, *, next_cursor=""):
    """One ``metGetDomainSearchResults`` response, which nests under a different key from the context one."""
    return {
        "data": {
            "universalDomainSearch": {
                "searchResults": list(search_results),
                "next": next_cursor,
                "totalResultCount": 0,
            }
        }
    }


def _hash_of(call):
    return json.loads(call[1]["extensions"])["persistedQuery"]["sha256Hash"]


def _is_domain_search(call):
    return call[1].get("operationName") == "metGetDomainSearchResults"


async def test_universal_search_games_reads_the_concepts_default_product_id():
    """A ``MobileGames`` hit's own id is a concept id; the store product id hangs off ``defaultProduct``,
    and a mapper that skipped it left the caller with no id in the ``store_product_id`` space at all."""
    expected_product_id = "UP1004-PPSA03420_00-GTAOSTANDALONE01"
    node = _concept(uuid.uuid4().hex, f"Ghost of {uuid.uuid4().hex}")
    node["defaultProduct"] = {"__typename": "Product", "id": expected_product_id}
    client = SocialClient(
        FakeSession(
            responses={"metGetContextSearchResults": _context_search([_domain(FULL_GAMES_DOMAIN, [_game_hit(node)])])}
        )
    )

    results = await client.universal_search_games("anything")

    assert results[0].default_product_id == expected_product_id


async def test_universal_search_games_pages_past_the_first_page_to_satisfy_the_limit():
    """PSN's first page carries far fewer hits than the domain total (``"GTA"``: 15 of 32), so a limit
    above the page size silently under-answered before this."""
    first_page_title = f"First {uuid.uuid4().hex}"
    second_page_title = f"Second {uuid.uuid4().hex}"
    session = FakeSession(
        responses={
            "metGetContextSearchResults": _context_search(
                [
                    _domain(
                        FULL_GAMES_DOMAIN, [_game_hit(_concept(uuid.uuid4().hex, first_page_title))], next_cursor="c1"
                    )
                ]
            ),
            "metGetDomainSearchResults": [
                _domain_page([_game_hit(_concept(uuid.uuid4().hex, second_page_title))], next_cursor="")
            ],
        }
    )
    client = SocialClient(session)

    results = await client.universal_search_games("anything", limit=2)

    assert [result.name for result in results] == [first_page_title, second_page_title]


async def test_game_paging_uses_the_games_hash_not_the_one_player_paging_uses():
    """An unverified persisted hash answers 200 with a body the parser reads as "no results", so a games
    page fetched under the social domain's hash would look like the search simply ran out."""
    session = FakeSession(
        responses={
            "metGetContextSearchResults": _context_search(
                [_domain(FULL_GAMES_DOMAIN, [_game_hit(_concept(uuid.uuid4().hex, "First"))], next_cursor="c1")]
            ),
            "metGetDomainSearchResults": [_domain_page([_game_hit(_concept(uuid.uuid4().hex, "Second"))])],
        }
    )
    client = SocialClient(session)

    await client.universal_search_games("anything", limit=2)

    paging_call = next(call for call in session.get_calls if _is_domain_search(call))
    assert _hash_of(paging_call) == "b51624299bd17b3799f77c9f097cc8887a04d3873f0329095976a841595bc902"
    assert _hash_of(paging_call) != "23ece284bf8bdc50bfa30a4d97fd4d733e723beb7a42dff8c1ee883f8461a2e1"


async def test_game_paging_names_the_domain_being_read_not_a_fixed_one():
    session = FakeSession(
        responses={
            "metGetContextSearchResults": _context_search(
                [_domain(ADD_ONS_DOMAIN, [_game_hit(_concept(uuid.uuid4().hex, "First"))], next_cursor="c1")]
            ),
            "metGetDomainSearchResults": [_domain_page([_game_hit(_concept(uuid.uuid4().hex, "Second"))])],
        }
    )
    client = SocialClient(session)

    await client.universal_search_games("anything", domain=ADD_ONS_DOMAIN, limit=2)

    paging_call = next(call for call in session.get_calls if _is_domain_search(call))
    assert json.loads(paging_call[1]["variables"])["searchDomain"] == ADD_ONS_DOMAIN


async def test_game_paging_stops_when_psn_runs_out_rather_than_looping_on_a_stale_cursor():
    session = FakeSession(
        responses={
            "metGetContextSearchResults": _context_search(
                [_domain(FULL_GAMES_DOMAIN, [_game_hit(_concept(uuid.uuid4().hex, "Only"))], next_cursor="c1")]
            ),
            "metGetDomainSearchResults": [_domain_page([], next_cursor="c1")],
        }
    )
    client = SocialClient(session)

    results = await client.universal_search_games("anything", limit=50)

    assert [result.name for result in results] == ["Only"]
    paging_calls = [call for call in session.get_calls if _is_domain_search(call)]
    assert len(paging_calls) == 1


async def test_game_paging_stops_at_the_page_cap_even_while_psn_still_offers_more():
    """Every page is a request on one user's own PSN token. An uncapped paginated fetch is how the admin
    OpenCritic sweep once spent a whole day's quota on its first run."""
    session = FakeSession(
        responses={
            "metGetContextSearchResults": _context_search(
                [_domain(FULL_GAMES_DOMAIN, [_game_hit(_concept(uuid.uuid4().hex, "First"))], next_cursor="more")]
            ),
            "metGetDomainSearchResults": [
                _domain_page([_game_hit(_concept(uuid.uuid4().hex, f"Page{page}"))], next_cursor="more")
                for page in range(MAX_GAME_SEARCH_PAGES + 5)
            ],
        }
    )
    client = SocialClient(session)

    results = await client.universal_search_games("anything", limit=50)

    paging_calls = [call for call in session.get_calls if _is_domain_search(call)]
    assert len(paging_calls) == MAX_GAME_SEARCH_PAGES
    assert len(results) == MAX_GAME_SEARCH_PAGES + 1


async def test_universal_search_games_does_not_page_when_the_first_page_already_meets_the_limit():
    session = FakeSession(
        responses={
            "metGetContextSearchResults": _context_search(
                [_domain(FULL_GAMES_DOMAIN, [_game_hit(_concept(uuid.uuid4().hex, "Only"))], next_cursor="c1")]
            )
        }
    )
    client = SocialClient(session)

    await client.universal_search_games("anything", limit=1)

    assert not [call for call in session.get_calls if _is_domain_search(call)]


async def test_universal_search_games_falls_back_to_the_invariant_name_when_the_locale_carries_none():
    expected_invariant_name = f"Invariant {uuid.uuid4().hex}"
    node = _concept(uuid.uuid4().hex, expected_invariant_name)
    node["name"] = None
    node["invariantName"] = expected_invariant_name
    client = SocialClient(
        FakeSession(
            responses={"metGetContextSearchResults": _context_search([_domain(FULL_GAMES_DOMAIN, [_game_hit(node)])])}
        )
    )

    results = await client.universal_search_games(expected_invariant_name)

    assert [result.name for result in results] == [expected_invariant_name]


async def test_universal_search_games_maps_platforms_classification_and_price():
    expected_base_price = f"${uuid.uuid4().int % 100}.99"
    expected_discounted_price = f"${uuid.uuid4().int % 100}.49"
    node = _concept(uuid.uuid4().hex, f"Priced {uuid.uuid4().hex}")
    node["platforms"] = ["PS4", "PS5"]
    node["price"] = {
        "basePrice": expected_base_price,
        "discountedPrice": expected_discounted_price,
        "isFree": False,
    }
    client = SocialClient(
        FakeSession(
            responses={"metGetContextSearchResults": _context_search([_domain(FULL_GAMES_DOMAIN, [_game_hit(node)])])}
        )
    )

    result = (await client.universal_search_games("priced"))[0]

    assert result.platforms == ("PS4", "PS5")
    assert result.classification == "Full Game"
    assert (result.price, result.discounted_price, result.is_free) == (
        expected_base_price,
        expected_discounted_price,
        False,
    )


async def test_universal_search_games_resolves_cover_art_by_role_preference_not_array_order():
    node = _concept(uuid.uuid4().hex, f"Arted {uuid.uuid4().hex}")
    expected_cover_url = f"https://cdn.invalid/{uuid.uuid4().hex}.jpg"
    node["media"] = [
        {"role": "SCREENSHOT", "type": "IMAGE", "url": f"https://cdn.invalid/{uuid.uuid4().hex}-shot.jpg"},
        {"role": "GAMEHUB_COVER_ART", "type": "IMAGE", "url": expected_cover_url},
    ]
    client = SocialClient(
        FakeSession(
            responses={"metGetContextSearchResults": _context_search([_domain(FULL_GAMES_DOMAIN, [_game_hit(node)])])}
        )
    )

    result = (await client.universal_search_games("arted"))[0]

    assert result.cover_image_url == expected_cover_url


async def test_universal_search_games_caps_the_page_at_the_requested_limit():
    requested_limit = 2
    hits = [_game_hit(_concept(uuid.uuid4().hex, f"Title {index}")) for index in range(requested_limit + 3)]
    client = SocialClient(
        FakeSession(responses={"metGetContextSearchResults": _context_search([_domain(FULL_GAMES_DOMAIN, hits)])})
    )

    results = await client.universal_search_games("many", limit=requested_limit)

    assert len(results) == requested_limit


async def test_universal_search_games_sends_the_game_context_and_a_hash_the_social_search_does_not_use():
    """Same operation name as the player search under a different context and a different persisted hash.
    Sending the social hash with the game context returns the wrong domain set, and nothing in the
    response says so."""
    session = FakeSession(responses={"metGetContextSearchResults": _context_search([])})
    client = SocialClient(session)

    await client.universal_search_games("anything")
    await client.universal_search_players("anybody")

    game_params = session.get_calls[0][1]
    player_params = session.get_calls[1][1]
    assert json.loads(game_params["variables"])["searchContext"] == GAME_SEARCH_CONTEXT
    assert json.loads(player_params["variables"])["searchContext"] == SOCIAL_SEARCH_CONTEXT
    assert game_params["extensions"] != player_params["extensions"]
