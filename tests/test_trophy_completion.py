"""Tests for ``curator.psn.trophy_completion``: the fuzzy title matcher (pure) and the soft-failing
``get_completion_map``/``get_completion_result`` orchestration, against a hand-written fake repository and
trophy-client factory -- the same ``SimpleNamespace``-standing-in-for-``Request`` style ``test_deps.py``
uses for ``require_preference``, since ``request.app.state.repository``/``.trophy_client_factory`` are the
only things this module actually touches on ``Request``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from curator.persistence.repository import LinkRecord
from curator.psn.errors import PsnAuthError
from curator.psn.models import TrophyCounts, TrophyTitle
from curator.psn.trophy_completion import (
    _resolve_completion,
    get_completion_map,
    get_completion_result,
    match_completion,
)

SUB = "sub-1"


def _title(name, progress=50, np_communication_id="NPWR1"):
    return TrophyTitle(
        name=name,
        np_communication_id=np_communication_id,
        platforms=("PS5",),
        progress=progress,
        earned=TrophyCounts(gold=1),
        defined=TrophyCounts(gold=2),
    )


class FakeRepository:
    """Stands in for Repository: in-memory dict of sub -> LinkRecord."""

    def __init__(self) -> None:
        self.links: dict[str, LinkRecord] = {}

    async def get_link(self, sub):
        return self.links.get(sub)


def _link(harvest_trophies: bool) -> LinkRecord:
    return LinkRecord(
        psn_account_id="psn-account-1",
        token_response_enc=b"encrypted",
        access_token_expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        refresh_token_expires_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        linked_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        last_verified_at=None,
        harvest_trophies=harvest_trophies,
    )


class FakeTrophyClient:
    def __init__(self, *, titles=None, raise_auth_error=False):
        self._titles = titles or []
        self.raise_auth_error = raise_auth_error

    async def trophy_titles(self, online_id=None, account_id=None, limit=100):
        if self.raise_auth_error:
            raise PsnAuthError("boom")
        return self._titles


class FakeTrophyClientFactory:
    """Records every ``sub`` requested; raises ``RuntimeError`` for any ``sub`` not explicitly linked."""

    def __init__(self):
        self.linked: dict[str, FakeTrophyClient] = {}
        self.calls: list[str] = []

    async def __call__(self, sub):
        self.calls.append(sub)
        client = self.linked.get(sub)
        if client is None:
            raise RuntimeError(f"No PSN link for user {sub!r}; cannot fetch trophies.")
        return client


def _request(repository: FakeRepository, trophy_client_factory: FakeTrophyClientFactory) -> SimpleNamespace:
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(repository=repository, trophy_client_factory=trophy_client_factory))
    )


# -- match_completion (pure) -------------------------------------------------------------------------


def test_match_completion_matches_identical_titles():
    titles = [_title("God of War Ragnarök", progress=87)]
    result = match_completion(titles, [("g1", "God of War Ragnarök")])
    assert result == {"g1": 87}


def test_match_completion_no_match_below_threshold():
    titles = [_title("Completely Unrelated Game", progress=87)]
    result = match_completion(titles, [("g1", "God of War Ragnarök")])
    assert result == {}


def test_match_completion_ignores_titles_with_no_progress():
    titles = [_title("God of War Ragnarök", progress=None)]
    result = match_completion(titles, [("g1", "God of War Ragnarök")])
    assert result == {}


def test_match_completion_ignores_titles_with_no_name():
    titles = [_title(None, progress=87)]
    result = match_completion(titles, [("g1", "God of War Ragnarök")])
    assert result == {}


def test_match_completion_picks_best_scoring_title_among_several():
    titles = [_title("Returnal", progress=10), _title("God of War Ragnarök", progress=87)]
    result = match_completion(titles, [("g1", "God of War Ragnarök")])
    assert result == {"g1": 87}


def test_match_completion_resolves_multiple_games_independently():
    titles = [_title("Returnal", progress=10), _title("God of War Ragnarök", progress=87)]
    result = match_completion(titles, [("g1", "God of War Ragnarök"), ("g2", "Returnal"), ("g3", "Unmatched")])
    assert result == {"g1": 87, "g2": 10}


def test_match_completion_is_one_to_one_only_the_better_match_keeps_the_title():
    # "Sample Game" and "Sample Games" both clear threshold against the single trophy title -- a base
    # game and a similarly-named edition/sequel is exactly the kind of collision this guards against.
    # Without one-to-one assignment, both would independently claim the same title's percentage.
    titles = [_title("Sample Game", progress=87)]
    result = match_completion(titles, [("weaker", "Sample Games"), ("exact", "Sample Game")], threshold=0.80)
    assert result == {"exact": 87}


def test_match_completion_one_to_one_assignment_is_score_ordered_not_input_ordered():
    # The weaker match is listed first in the input; the exact match must still win the title, proving
    # assignment is sorted by score rather than "first candidate in iteration order wins."
    titles = [_title("Sample Game", progress=87)]
    result = match_completion(titles, [("weaker", "Sample Games"), ("exact", "Sample Game")], threshold=0.80)
    assert "weaker" not in result
    assert result.get("exact") == 87


def test_match_completion_title_claimed_by_one_game_is_unavailable_to_another():
    # Once "Sample Game" is claimed by the exact match, a second, unrelated game must not also be able to
    # claim it even if it would otherwise have cleared threshold against it in isolation.
    titles = [_title("Sample Game", progress=87)]
    result = match_completion(
        titles,
        [("exact", "Sample Game"), ("also-close", "Sample Game HD")],
        threshold=0.80,
    )
    assert result == {"exact": 87}


# -- _resolve_completion (pure: exact-by-id lookup, fuzzy fallback for the rest) -----------------------


def test_resolve_completion_exact_lookup_by_persisted_np_communication_id():
    titles = [_title("Some Trophy Title Name", progress=75, np_communication_id="NPWR00001_00")]
    # canonical_title is deliberately nothing like the trophy title's name -- proves this resolves by id,
    # not by falling through to a fuzzy match that happens to also succeed.
    result = _resolve_completion(titles, [("g1", "Totally Different Catalog Title", "NPWR00001_00")])
    assert result == {"g1": 75}


def test_resolve_completion_falls_back_to_fuzzy_when_no_persisted_id():
    titles = [_title("God of War Ragnarök", progress=87, np_communication_id="NPWR00001_00")]
    result = _resolve_completion(titles, [("g1", "God of War Ragnarök", None)])
    assert result == {"g1": 87}


def test_resolve_completion_persisted_id_not_found_in_current_titles_falls_back_to_fuzzy():
    # A stale/wrong persisted id (or PSN simply not returning that title this call) must not produce a
    # blank result if the fuzzy fallback would otherwise have found a confident match.
    titles = [_title("God of War Ragnarök", progress=87, np_communication_id="NPWR00002_00")]
    result = _resolve_completion(titles, [("g1", "God of War Ragnarök", "NPWR00099_00")])
    assert result == {"g1": 87}


def test_resolve_completion_exact_match_withholds_its_title_from_the_fuzzy_pool():
    # "g1" exactly claims the only trophy title via its persisted id. "g2" has no persisted id and would
    # otherwise fuzzy-match that same title (identical name) -- it must not also claim it.
    titles = [_title("Same Name", progress=75, np_communication_id="NPWR00001_00")]
    result = _resolve_completion(titles, [("g1", "Anything", "NPWR00001_00"), ("g2", "Same Name", None)])
    assert result == {"g1": 75}


def test_resolve_completion_mixes_exact_and_fuzzy_across_different_games():
    titles = [
        _title("God of War Ragnarök", progress=87, np_communication_id="NPWR00001_00"),
        _title("Returnal", progress=42, np_communication_id="NPWR00002_00"),
    ]
    result = _resolve_completion(
        titles,
        [("g1", "irrelevant catalog title", "NPWR00001_00"), ("g2", "Returnal", None)],
    )
    assert result == {"g1": 87, "g2": 42}


# -- get_completion_map / get_completion_result (soft-failing orchestration) -------------------------


async def test_no_games_returns_empty_without_touching_repository():
    repository = FakeRepository()
    factory = FakeTrophyClientFactory()

    result = await get_completion_map(_request(repository, factory), SUB, [])

    assert result == {}
    assert factory.calls == []


async def test_no_link_returns_empty():
    repository = FakeRepository()
    factory = FakeTrophyClientFactory()

    result = await get_completion_map(_request(repository, factory), SUB, [("g1", "Title", None)])

    assert result == {}
    assert factory.calls == []  # never reaches PSN once the link check fails


async def test_harvest_trophies_disabled_returns_empty():
    repository = FakeRepository()
    repository.links[SUB] = _link(harvest_trophies=False)
    factory = FakeTrophyClientFactory()

    result = await get_completion_map(_request(repository, factory), SUB, [("g1", "Title", None)])

    assert result == {}
    assert factory.calls == []


async def test_broken_link_runtime_error_returns_empty():
    repository = FakeRepository()
    repository.links[SUB] = _link(harvest_trophies=True)
    factory = FakeTrophyClientFactory()  # SUB not registered -> factory raises RuntimeError

    result = await get_completion_map(_request(repository, factory), SUB, [("g1", "Title", None)])

    assert result == {}


async def test_psn_auth_error_returns_empty():
    repository = FakeRepository()
    repository.links[SUB] = _link(harvest_trophies=True)
    factory = FakeTrophyClientFactory()
    factory.linked[SUB] = FakeTrophyClient(raise_auth_error=True)

    result = await get_completion_map(_request(repository, factory), SUB, [("g1", "Title", None)])

    assert result == {}


async def test_happy_path_matches_games_via_fuzzy_fallback():
    repository = FakeRepository()
    repository.links[SUB] = _link(harvest_trophies=True)
    factory = FakeTrophyClientFactory()
    factory.linked[SUB] = FakeTrophyClient(
        titles=[_title("God of War Ragnarök", progress=87, np_communication_id="NPWR1")]
    )

    result = await get_completion_map(_request(repository, factory), SUB, [("g1", "God of War Ragnarök", None)])

    assert result == {"g1": 87}


async def test_happy_path_matches_games_via_exact_persisted_id():
    repository = FakeRepository()
    repository.links[SUB] = _link(harvest_trophies=True)
    factory = FakeTrophyClientFactory()
    factory.linked[SUB] = FakeTrophyClient(titles=[_title("Anything", progress=87, np_communication_id="NPWR1")])

    result = await get_completion_map(
        _request(repository, factory), SUB, [("g1", "Catalog title unrelated to trophy name", "NPWR1")]
    )

    assert result == {"g1": 87}


async def test_completion_result_reports_available_on_success():
    repository = FakeRepository()
    repository.links[SUB] = _link(harvest_trophies=True)
    factory = FakeTrophyClientFactory()
    factory.linked[SUB] = FakeTrophyClient(titles=[_title("Returnal", progress=42, np_communication_id="NPWR2")])

    result = await get_completion_result(_request(repository, factory), SUB, [("g1", "Returnal", None)])

    assert result.available is True
    assert result.by_game == {"g1": 42}


async def test_completion_result_reports_unavailable_when_no_link():
    repository = FakeRepository()
    factory = FakeTrophyClientFactory()

    result = await get_completion_result(_request(repository, factory), SUB, [("g1", "Returnal", None)])

    assert result.available is False
    assert result.by_game == {}


async def test_completion_result_reports_unavailable_on_auth_error():
    repository = FakeRepository()
    repository.links[SUB] = _link(harvest_trophies=True)
    factory = FakeTrophyClientFactory()
    factory.linked[SUB] = FakeTrophyClient(raise_auth_error=True)

    result = await get_completion_result(_request(repository, factory), SUB, [("g1", "Returnal", None)])

    assert result.available is False
    assert result.by_game == {}
