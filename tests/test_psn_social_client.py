"""Tests for SocialClient, using a hand-written fake session (no network, no credentials).

Ported from ``psnpy``'s ``test_social.py``/``test_capabilities.py``.
"""

from __future__ import annotations

import json

import pytest

from curator.psn._graphql import (
    DATA_KEY,
    EXTENSIONS_PARAM,
    GRAPHQL_URL,
    OPERATION_NAME_PARAM,
    PERSISTED_QUERY_KEY,
    SHA256_HASH_KEY,
    VARIABLES_PARAM,
)
from curator.psn._identity import (
    ACCOUNT_ID_KEY,
    MY_ACCOUNT_URL,
    ONLINE_ID_KEY,
    PROFILE_KEY,
    SELF_PATH_ID,
    legacy_profile_url,
    profiles_url,
)
from curator.psn._media import GAMEHUB_COVER_ART_ROLE, IMAGE_MEDIA_TYPE, ROLE_KEY, TYPE_KEY, URL_KEY
from curator.psn._product_node import (
    BASE_PRICE_KEY,
    CLASSIFICATION_KEY,
    DEFAULT_PRODUCT_KEY,
    DISCOUNTED_PRICE_KEY,
    ID_KEY,
    INVARIANT_NAME_KEY,
    IS_FREE_KEY,
    MEDIA_KEY,
    NAME_KEY,
    PLATFORMS_KEY,
    PRICE_KEY,
    TYPENAME_KEY,
)
from curator.psn.models import AccountDevice, Friendship, PlayerSearchResult, Profile, ProfileShareLink, SocialUser
from curator.psn.social_client import (
    ABOUT_ME_KEY,
    ACCOUNT_DEVICES_KEY,
    ACTIVATION_DATE_KEY,
    ACTIVATION_TYPE_KEY,
    ADD_ONS_DOMAIN,
    AVATAR_URL_KEY,
    AVATAR_URLS_KEY,
    BLOCK_LIST_KEY,
    CONTEXT_SEARCH_OPERATION,
    DEVICE_ID_KEY,
    DEVICE_NAME_KEY,
    DEVICE_TYPE_KEY,
    DOMAIN_KEY,
    DOMAIN_SEARCH_OPERATION,
    FRIEND_RELATION_KEY,
    FRIENDS_COUNT_KEY,
    FRIENDS_KEY,
    FULL_GAMES_DOMAIN,
    GAME_DOMAIN_SEARCH_HASH,
    GAME_SEARCH_CONTEXT,
    GROUP_ID_KEY,
    GROUPS_KEY,
    INCLUDE_FIELDS_PARAM,
    IS_OFFICIALLY_VERIFIED_KEY,
    IS_PS_PLUS_KEY,
    LIMIT_PARAM,
    MAX_CHAT_GROUPS,
    MAX_GAME_SEARCH_PAGES,
    MUTUAL_FRIENDS_COUNT_KEY,
    NEXT_KEY,
    RECEIVED_REQUESTS_KEY,
    RESULT_KEY,
    RESULTS_KEY,
    SEARCH_CONTEXT_VARIABLE,
    SEARCH_DOMAIN_VARIABLE,
    SEARCH_RESULTS_KEY,
    SETTINGS_KEY,
    SHARE_IMAGE_URL_DESTINATION_KEY,
    SHARE_IMAGE_URL_KEY,
    SHARE_URL_KEY,
    SOCIAL_DOMAIN_SEARCH_HASH,
    SOCIAL_SEARCH_CONTEXT,
    UNIVERSAL_CONTEXT_SEARCH_KEY,
    UNIVERSAL_DOMAIN_SEARCH_KEY,
    SocialClient,
    available_to_play_url,
    blocks_url,
    friends_url,
    friendship_summary_url,
    my_chat_groups_url,
    received_requests_url,
    share_profile_url,
)
from curator.psn.title_platform import PS4, PS5
from test_values import (
    lowercase_token,
    new_account_id,
    new_concept_id,
    new_cover_image_url,
    new_device_id,
    new_game_title,
    new_group_id,
    new_online_id,
    new_opaque_token,
    new_positive_count,
    new_store_product_id,
    new_utc_instant,
)


class FakeResponse:
    def __init__(self, body):
        self._body = body

    def json(self):
        return self._body


class FakeSession:
    """Answers by exact URL, and GraphQL calls by operation name. A list body is a queue of pages.

    Any account's online id resolves from ``online_ids``; the devices list is the account URL read with
    ``includeFields``, and the caller's own account id is the same URL read without it.
    """

    def __init__(self, *, own_account_id=None, routes=None, operations=None, online_ids=None, devices_body=None):
        self.own_account_id = own_account_id or new_account_id()
        self._routes = dict(routes or {})
        self._operations = dict(operations or {})
        self._online_ids = dict(online_ids or {})
        self._devices_body = devices_body
        self.get_calls: list[tuple[str, dict]] = []

    async def get(self, url, params=None, headers=None):
        params = params or {}
        self.get_calls.append((url, params))
        if url == MY_ACCOUNT_URL:
            if INCLUDE_FIELDS_PARAM in params:
                return FakeResponse(self._devices_body)
            return FakeResponse({ACCOUNT_ID_KEY: self.own_account_id})
        if url == GRAPHQL_URL:
            body = self._operations.get(params[OPERATION_NAME_PARAM], {})
            return FakeResponse((body.pop(0) if body else {}) if isinstance(body, list) else body)
        for account_id, online_id in self._online_ids.items():
            if url == profiles_url(account_id):
                return FakeResponse({ONLINE_ID_KEY: online_id})
        return FakeResponse(self._routes[url])

    async def run_with_reauth(self, operation):
        return await operation()


def _user():
    return SocialUser(account_id=new_account_id(), online_id=new_online_id())


async def test_friends_resolves_online_ids():
    first, second = _user(), _user()
    session = FakeSession(
        routes={friends_url(SELF_PATH_ID): {FRIENDS_KEY: [first.account_id, second.account_id]}},
        online_ids={first.account_id: first.online_id, second.account_id: second.online_id},
    )

    friends = await SocialClient(session).friends()

    assert friends == [first, second]


async def test_blocked_resolves_online_ids():
    user = _user()
    session = FakeSession(
        routes={blocks_url(): {BLOCK_LIST_KEY: [user.account_id]}}, online_ids={user.account_id: user.online_id}
    )

    blocked = await SocialClient(session).blocked()

    assert blocked == [user]


async def test_available_to_play_resolves_online_ids():
    account_id, online_id = new_account_id(), new_online_id()
    body = {SETTINGS_KEY: [{ACCOUNT_ID_KEY: account_id}]}
    client = SocialClient(FakeSession(routes={available_to_play_url(): body}, online_ids={account_id: online_id}))

    result = await client.available_to_play()

    assert result == [SocialUser(account_id=account_id, online_id=online_id)]


async def test_friend_requests_resolves_online_ids():
    account_id, online_id = new_account_id(), new_online_id()
    body = {RECEIVED_REQUESTS_KEY: [{ACCOUNT_ID_KEY: account_id}]}
    client = SocialClient(FakeSession(routes={received_requests_url(): body}, online_ids={account_id: online_id}))

    result = await client.friend_requests()

    assert result == [SocialUser(account_id=account_id, online_id=online_id)]


async def test_chat_group_ids_reads_the_callers_group_memberships():
    first_group_id, second_group_id = new_group_id(), new_group_id()
    body = {GROUPS_KEY: [{GROUP_ID_KEY: first_group_id}, {GROUP_ID_KEY: second_group_id}, {}]}
    session = FakeSession(routes={my_chat_groups_url(): body})

    group_ids = await SocialClient(session).chat_group_ids()

    assert group_ids == [first_group_id, second_group_id]
    assert session.get_calls[-1][1][LIMIT_PARAM] == MAX_CHAT_GROUPS


async def test_friendship_maps_fields():
    account_id = new_account_id()
    expected = Friendship(
        relation=lowercase_token(), friends_count=new_positive_count(), mutual_friends_count=new_positive_count()
    )
    body = {
        FRIEND_RELATION_KEY: expected.relation,
        FRIENDS_COUNT_KEY: expected.friends_count,
        MUTUAL_FRIENDS_COUNT_KEY: expected.mutual_friends_count,
    }
    client = SocialClient(FakeSession(routes={friendship_summary_url(account_id): body}))

    result = await client.friendship(account_id=account_id)

    assert result == expected


async def test_friendship_requires_a_target():
    with pytest.raises(ValueError, match="requires a target"):
        await SocialClient(FakeSession()).friendship()


async def test_profile_never_hydrates_personal_detail():
    online_id = new_online_id()
    expected = Profile(about_me=lowercase_token(), avatars=(new_cover_image_url(),), is_officially_verified=True)
    body = {
        PROFILE_KEY: {
            ABOUT_ME_KEY: expected.about_me,
            AVATAR_URLS_KEY: [{AVATAR_URL_KEY: expected.avatars[0]}],
            IS_OFFICIALLY_VERIFIED_KEY: True,
        }
    }
    session = FakeSession(routes={legacy_profile_url(online_id): body})
    session._online_ids[session.own_account_id] = online_id

    profile = await SocialClient(session).profile()

    assert profile == expected


async def test_is_blocked_true_for_a_listed_account():
    account_id = new_account_id()
    client = SocialClient(FakeSession(routes={blocks_url(): {BLOCK_LIST_KEY: [account_id]}}))

    assert await client.is_blocked(account_id=account_id) is True


async def test_is_blocked_false_for_an_unlisted_account():
    client = SocialClient(FakeSession(routes={blocks_url(): {BLOCK_LIST_KEY: [new_account_id()]}}))

    assert await client.is_blocked(account_id=new_account_id()) is False


async def test_is_blocked_requires_a_target():
    with pytest.raises(ValueError, match="requires a target"):
        await SocialClient(FakeSession()).is_blocked()


async def test_devices_maps_fields():
    expected = AccountDevice(
        device_id=new_device_id(),
        device_type=PS5,
        device_name=new_game_title(),
        activation_type=lowercase_token(),
        activation_date=new_utc_instant().isoformat(),
    )
    body = {
        ACCOUNT_DEVICES_KEY: [
            {
                DEVICE_ID_KEY: expected.device_id,
                DEVICE_TYPE_KEY: expected.device_type,
                DEVICE_NAME_KEY: expected.device_name,
                ACTIVATION_TYPE_KEY: expected.activation_type,
                ACTIVATION_DATE_KEY: expected.activation_date,
            }
        ]
    }

    devices = await SocialClient(FakeSession(devices_body=body)).devices()

    assert devices == [expected]


async def test_share_link_maps_fields():
    session = FakeSession()
    expected = ProfileShareLink(
        share_url=new_cover_image_url(),
        share_image_url=new_cover_image_url(),
        share_image_url_destination=new_cover_image_url(),
    )
    session._routes[share_profile_url(session.own_account_id)] = {
        SHARE_URL_KEY: expected.share_url,
        SHARE_IMAGE_URL_KEY: expected.share_image_url,
        SHARE_IMAGE_URL_DESTINATION_KEY: expected.share_image_url_destination,
    }

    result = await SocialClient(session).share_link()

    assert result == expected


async def test_universal_search_players_maps_first_page():
    expected = PlayerSearchResult(account_id=new_account_id(), online_id=new_online_id(), is_ps_plus=True)
    player = {ACCOUNT_ID_KEY: expected.account_id, ONLINE_ID_KEY: expected.online_id, IS_PS_PLUS_KEY: True}
    context_response = _context_search([{SEARCH_RESULTS_KEY: [{RESULT_KEY: player}]}])
    client = SocialClient(FakeSession(operations={CONTEXT_SEARCH_OPERATION: context_response}))

    results = await client.universal_search_players(expected.online_id)

    assert results == [expected]


def _domain(domain, search_results, *, next_cursor=None):
    """One entry of ``data.universalContextSearch.results``, in the gateway's own shape."""
    return {DOMAIN_KEY: domain, SEARCH_RESULTS_KEY: list(search_results), NEXT_KEY: next_cursor}


def _context_search(domains):
    return {DATA_KEY: {UNIVERSAL_CONTEXT_SEARCH_KEY: {RESULTS_KEY: list(domains)}}}


def _domain_page(search_results, *, next_cursor=None):
    """One ``metGetDomainSearchResults`` response, which nests under a different key from the context one."""
    return {DATA_KEY: {UNIVERSAL_DOMAIN_SEARCH_KEY: {SEARCH_RESULTS_KEY: list(search_results), NEXT_KEY: next_cursor}}}


def _game_hit(result, *, item_id=None):
    """One ``searchResults`` entry: a wrapper carrying its own id plus the ``result`` node under it."""
    return {ID_KEY: item_id or new_opaque_token(), RESULT_KEY: result}


def _concept(name=None):
    """A concept result node in the shape ``psnawp``'s ``games_search_datatypes`` declares."""
    title = name or new_game_title()
    return {
        TYPENAME_KEY: lowercase_token(),
        ID_KEY: new_concept_id(),
        NAME_KEY: title,
        INVARIANT_NAME_KEY: title,
        PLATFORMS_KEY: [PS5],
        MEDIA_KEY: [{ROLE_KEY: GAMEHUB_COVER_ART_ROLE, TYPE_KEY: IMAGE_MEDIA_TYPE, URL_KEY: new_cover_image_url()}],
        CLASSIFICATION_KEY: lowercase_token(),
    }


def _games_session(domains, *, pages=None):
    operations = {CONTEXT_SEARCH_OPERATION: _context_search(domains)}
    if pages is not None:
        operations[DOMAIN_SEARCH_OPERATION] = pages
    return FakeSession(operations=operations)


def _hash_of(call):
    return json.loads(call[1][EXTENSIONS_PARAM])[PERSISTED_QUERY_KEY][SHA256_HASH_KEY]


def _domain_search_calls(session):
    return [call for call in session.get_calls if call[1].get(OPERATION_NAME_PARAM) == DOMAIN_SEARCH_OPERATION]


async def test_universal_search_games_reads_the_title_from_the_result_node_not_the_search_item():
    """The search item carries an ``id`` of its own beside the ``result`` node it wraps. Reading the
    wrapper yields an id nothing joins on and no title at all, which is how an earlier pass got ids right
    and ``None`` for every name."""
    node = _concept()
    wrapper_id = new_opaque_token()
    client = SocialClient(_games_session([_domain(FULL_GAMES_DOMAIN, [_game_hit(node, item_id=wrapper_id)])]))

    results = await client.universal_search_games(node[NAME_KEY])

    assert [(result.id, result.name) for result in results] == [(node[ID_KEY], node[NAME_KEY])]
    assert results[0].id != wrapper_id


async def test_universal_search_games_selects_the_container_by_its_domain_label_not_its_position():
    """PSNAWP indexes this list positionally (FULL_GAMES = 0). Nothing in the payload promises that order,
    so the add-ons container arriving first must not be served as the full-games result."""
    full_game = _concept()
    client = SocialClient(
        _games_session(
            [_domain(ADD_ONS_DOMAIN, [_game_hit(_concept())]), _domain(FULL_GAMES_DOMAIN, [_game_hit(full_game)])]
        )
    )

    results = await client.universal_search_games(full_game[NAME_KEY])

    assert [result.name for result in results] == [full_game[NAME_KEY]]


async def test_universal_search_games_reads_the_add_ons_domain_when_asked_for_it():
    add_on = _concept()
    client = SocialClient(
        _games_session(
            [_domain(FULL_GAMES_DOMAIN, [_game_hit(_concept())]), _domain(ADD_ONS_DOMAIN, [_game_hit(add_on)])]
        )
    )

    results = await client.universal_search_games(add_on[NAME_KEY], domain=ADD_ONS_DOMAIN)

    assert [result.name for result in results] == [add_on[NAME_KEY]]


async def test_universal_search_games_returns_nothing_when_psn_sends_no_container_for_that_domain():
    """Indexing the list positionally raises IndexError here; matching the label degrades to empty."""
    client = SocialClient(_games_session([_domain(FULL_GAMES_DOMAIN, [_game_hit(_concept())])]))

    results = await client.universal_search_games(lowercase_token(), domain=ADD_ONS_DOMAIN)

    assert results == []


async def test_universal_search_games_reads_the_concepts_default_product_id():
    """A ``MobileGames`` hit's own id is a concept id; the store product id hangs off ``defaultProduct``,
    and a mapper that skipped it left the caller with no id in the ``store_product_id`` space at all."""
    expected_product_id = new_store_product_id()
    node = _concept()
    node[DEFAULT_PRODUCT_KEY] = {ID_KEY: expected_product_id}
    client = SocialClient(_games_session([_domain(FULL_GAMES_DOMAIN, [_game_hit(node)])]))

    results = await client.universal_search_games(lowercase_token())

    assert results[0].default_product_id == expected_product_id


async def test_universal_search_games_pages_past_the_first_page_to_satisfy_the_limit():
    """PSN's first page carries far fewer hits than the domain total (``"GTA"``: 15 of 32), so a limit
    above the page size silently under-answered before this."""
    first, second = _concept(), _concept()
    session = _games_session(
        [_domain(FULL_GAMES_DOMAIN, [_game_hit(first)], next_cursor=new_opaque_token())],
        pages=[_domain_page([_game_hit(second)])],
    )

    results = await SocialClient(session).universal_search_games(lowercase_token(), limit=2)

    assert [result.name for result in results] == [first[NAME_KEY], second[NAME_KEY]]


async def test_game_paging_uses_the_games_hash_not_the_one_player_paging_uses():
    """An unverified persisted hash answers 200 with a body the parser reads as "no results", so a games
    page fetched under the social domain's hash would look like the search simply ran out."""
    session = _games_session(
        [_domain(FULL_GAMES_DOMAIN, [_game_hit(_concept())], next_cursor=new_opaque_token())],
        pages=[_domain_page([_game_hit(_concept())])],
    )

    await SocialClient(session).universal_search_games(lowercase_token(), limit=2)

    [paging_call] = _domain_search_calls(session)
    assert _hash_of(paging_call) == GAME_DOMAIN_SEARCH_HASH
    assert _hash_of(paging_call) != SOCIAL_DOMAIN_SEARCH_HASH


async def test_game_paging_names_the_domain_being_read_not_a_fixed_one():
    session = _games_session(
        [_domain(ADD_ONS_DOMAIN, [_game_hit(_concept())], next_cursor=new_opaque_token())],
        pages=[_domain_page([_game_hit(_concept())])],
    )

    await SocialClient(session).universal_search_games(lowercase_token(), domain=ADD_ONS_DOMAIN, limit=2)

    [paging_call] = _domain_search_calls(session)
    assert json.loads(paging_call[1][VARIABLES_PARAM])[SEARCH_DOMAIN_VARIABLE] == ADD_ONS_DOMAIN


async def test_game_paging_stops_when_psn_runs_out_rather_than_looping_on_a_stale_cursor():
    cursor = new_opaque_token()
    only = _concept()
    session = _games_session(
        [_domain(FULL_GAMES_DOMAIN, [_game_hit(only)], next_cursor=cursor)],
        pages=[_domain_page([], next_cursor=cursor)],
    )

    results = await SocialClient(session).universal_search_games(lowercase_token(), limit=50)

    assert [result.name for result in results] == [only[NAME_KEY]]
    assert len(_domain_search_calls(session)) == 1


async def test_game_paging_stops_at_the_page_cap_even_while_psn_still_offers_more():
    """Every page is a request on one user's own PSN token. An uncapped paginated fetch is how the admin
    OpenCritic sweep once spent a whole day's quota on its first run."""
    cursor = new_opaque_token()
    session = _games_session(
        [_domain(FULL_GAMES_DOMAIN, [_game_hit(_concept())], next_cursor=cursor)],
        pages=[_domain_page([_game_hit(_concept())], next_cursor=cursor) for _ in range(MAX_GAME_SEARCH_PAGES + 5)],
    )

    results = await SocialClient(session).universal_search_games(lowercase_token(), limit=50)

    assert len(_domain_search_calls(session)) == MAX_GAME_SEARCH_PAGES
    assert len(results) == MAX_GAME_SEARCH_PAGES + 1


async def test_universal_search_games_does_not_page_when_the_first_page_already_meets_the_limit():
    session = _games_session([_domain(FULL_GAMES_DOMAIN, [_game_hit(_concept())], next_cursor=new_opaque_token())])

    await SocialClient(session).universal_search_games(lowercase_token(), limit=1)

    assert _domain_search_calls(session) == []


async def test_universal_search_games_falls_back_to_the_invariant_name_when_the_locale_carries_none():
    node = _concept()
    node[NAME_KEY] = None
    client = SocialClient(_games_session([_domain(FULL_GAMES_DOMAIN, [_game_hit(node)])]))

    results = await client.universal_search_games(node[INVARIANT_NAME_KEY])

    assert [result.name for result in results] == [node[INVARIANT_NAME_KEY]]


async def test_universal_search_games_maps_platforms_classification_and_price():
    base_price, discounted_price = lowercase_token(), lowercase_token()
    node = _concept()
    node[PLATFORMS_KEY] = [PS4, PS5]
    node[PRICE_KEY] = {BASE_PRICE_KEY: base_price, DISCOUNTED_PRICE_KEY: discounted_price, IS_FREE_KEY: False}
    client = SocialClient(_games_session([_domain(FULL_GAMES_DOMAIN, [_game_hit(node)])]))

    result = (await client.universal_search_games(lowercase_token()))[0]

    assert result.platforms == (PS4, PS5)
    assert result.classification == node[CLASSIFICATION_KEY]
    assert (result.price, result.discounted_price, result.is_free) == (base_price, discounted_price, False)


async def test_universal_search_games_resolves_cover_art_by_role_preference_not_array_order():
    node = _concept()
    expected_cover_url = new_cover_image_url()
    node[MEDIA_KEY] = [
        {ROLE_KEY: lowercase_token().upper(), TYPE_KEY: IMAGE_MEDIA_TYPE, URL_KEY: new_cover_image_url()},
        {ROLE_KEY: GAMEHUB_COVER_ART_ROLE, TYPE_KEY: IMAGE_MEDIA_TYPE, URL_KEY: expected_cover_url},
    ]
    client = SocialClient(_games_session([_domain(FULL_GAMES_DOMAIN, [_game_hit(node)])]))

    result = (await client.universal_search_games(lowercase_token()))[0]

    assert result.cover_image_url == expected_cover_url


async def test_universal_search_games_caps_the_page_at_the_requested_limit():
    requested_limit = 2
    hits = [_game_hit(_concept()) for _ in range(requested_limit + 3)]
    client = SocialClient(_games_session([_domain(FULL_GAMES_DOMAIN, hits)]))

    results = await client.universal_search_games(lowercase_token(), limit=requested_limit)

    assert len(results) == requested_limit


async def test_universal_search_games_sends_the_game_context_and_a_hash_the_social_search_does_not_use():
    """Same operation name as the player search under a different context and a different persisted hash.
    Sending the social hash with the game context returns the wrong domain set, and nothing in the
    response says so."""
    session = FakeSession(operations={CONTEXT_SEARCH_OPERATION: _context_search([])})
    client = SocialClient(session)

    await client.universal_search_games(lowercase_token())
    await client.universal_search_players(lowercase_token())

    game_params = session.get_calls[0][1]
    player_params = session.get_calls[1][1]
    assert json.loads(game_params[VARIABLES_PARAM])[SEARCH_CONTEXT_VARIABLE] == GAME_SEARCH_CONTEXT
    assert json.loads(player_params[VARIABLES_PARAM])[SEARCH_CONTEXT_VARIABLE] == SOCIAL_SEARCH_CONTEXT
    assert _hash_of(session.get_calls[0]) != _hash_of(session.get_calls[1])
