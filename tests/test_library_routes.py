"""Tests for POST /library/refresh, using create_app() with a fake QueuePublisher."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from curator.app import create_app
from curator.persistence.crypto import TokenCrypto
from test_routes import (
    FakeAgentFactory,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _seed_link,
)
from test_trophy_routes import FakeTrophyClient, FakeTrophyClientFactory


class FakePublisher:
    def __init__(self, run_id="run-1"):
        self._run_id = run_id
        self.library_refresh_calls = []

    async def publish_library_refresh(self, identity_sub):
        self.library_refresh_calls.append(identity_sub)
        return self._run_id


class FakeJobRun:
    def __init__(self, run_id, kind, identity_sub, status, error=None, result_summary=None, updated_at=None):
        self.run_id = run_id
        self.kind = kind
        self.identity_sub = identity_sub
        self.status = status
        self.error = error
        self.result_summary = result_summary
        self.updated_at = updated_at if updated_at is not None else datetime.now(timezone.utc)


class FakeJobRunsRepository:
    def __init__(self, runs=None):
        self.runs: dict[str, FakeJobRun] = {run.run_id: run for run in (runs or [])}
        self.failed_calls = []

    async def get(self, run_id):
        return self.runs.get(run_id)

    async def find_active_run(self, identity_sub, kind):
        candidates = [
            run
            for run in self.runs.values()
            if run.identity_sub == identity_sub and run.kind == kind and run.status not in ("succeeded", "failed")
        ]
        return max(candidates, key=lambda run: run.updated_at, default=None)

    async def mark_failed(self, run_id, error):
        self.failed_calls.append((run_id, error))
        self.runs[run_id].status = "failed"
        self.runs[run_id].error = error


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
        np_communication_id=None,
        percent_completed=None,
        source="psn",
        cover_image_url=None,
        platforms=(),
    ):
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
        self.np_communication_id = np_communication_id
        self.percent_completed = percent_completed
        self.source = source
        self.cover_image_url = cover_image_url
        self.platforms = platforms


_SORT_ATTRS = {
    "title": "title",
    "category": "category",
    "rawg_rating": "rawg_rating",
    "opencritic_rating": "opencritic_rating",
    "psn_rating": "psn_rating",
    "percent_completed": "percent_completed",
}


class FakeLibraryRepository:
    """Hand-written fake that actually implements search/category/sort/paging in memory, so tests
    against it exercise real filter/sort/page behavior, not just a passthrough."""

    def __init__(self, games_by_sub=None):
        self._games_by_sub = games_by_sub or {}

    async def list_entries_with_enrichment(
        self, identity_sub, *, search=None, category=None, sort="title", sort_dir="asc", limit=20, offset=0
    ):
        games = list(self._games_by_sub.get(identity_sub, []))
        if search:
            games = [g for g in games if search.lower() in g.title.lower()]
        if category:
            games = [g for g in games if g.category == category]

        attr = _SORT_ATTRS[sort]
        reverse = sort_dir == "desc"
        games.sort(key=lambda g: (getattr(g, attr) is None, getattr(g, attr), g.title), reverse=False)
        if reverse:
            non_null = [g for g in games if getattr(g, attr) is not None]
            non_null.sort(key=lambda g: getattr(g, attr), reverse=True)
            null = [g for g in games if getattr(g, attr) is None]
            games = non_null + null

        total = len(games)
        return games[offset : offset + limit], total

    async def list_categories(self, identity_sub):
        games = self._games_by_sub.get(identity_sub, [])
        return sorted({g.category for g in games if g.category is not None})


def _build(job_runs_repository=None, library_repository=None, repository=None, trophy_client_factory=None):
    repository = repository if repository is not None else FakeRepository()
    token_crypto = TokenCrypto(Fernet.generate_key())
    validator = FakeTokenValidator()
    publisher = FakePublisher()
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=validator,
        trophy_client_factory=trophy_client_factory or FakeTrophyClientFactory(),
    )
    app.state.queue_publisher = publisher
    app.state.job_runs_repository = job_runs_repository or FakeJobRunsRepository()
    app.state.library_repository = library_repository or FakeLibraryRepository()
    return TestClient(app), validator, publisher


def test_requires_bearer_token():
    client, _validator, _publisher = _build()

    response = client.post("/library/refresh")

    assert response.status_code == 401


def test_publishes_for_the_callers_own_sub_and_returns_run_id():
    client, validator, publisher = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/library/refresh", headers=_bearer("token-a"))

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1"}
    assert publisher.library_refresh_calls == ["sub-a"]


def test_duplicate_refresh_returns_existing_run_id_instead_of_publishing_again():
    active = FakeJobRun("run-existing", "library_refresh", "sub-a", "running")
    client, validator, publisher = _build(FakeJobRunsRepository([active]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/library/refresh", headers=_bearer("token-a"))

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-existing"}
    assert publisher.library_refresh_calls == []


def test_duplicate_refresh_guard_is_scoped_to_the_caller_and_kind():
    other_users_run = FakeJobRun("run-other", "library_refresh", "sub-b", "running")
    enrichment_run = FakeJobRun("run-enrichment", "enrichment", None, "running")
    client, validator, publisher = _build(FakeJobRunsRepository([other_users_run, enrichment_run]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/library/refresh", headers=_bearer("token-a"))

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1"}
    assert publisher.library_refresh_calls == ["sub-a"]


def test_a_terminal_run_does_not_block_a_new_refresh():
    finished = FakeJobRun("run-old", "library_refresh", "sub-a", "succeeded")
    client, validator, publisher = _build(FakeJobRunsRepository([finished]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/library/refresh", headers=_bearer("token-a"))

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1"}
    assert publisher.library_refresh_calls == ["sub-a"]


def test_a_stale_non_terminal_run_is_superseded_not_returned():

    stale = FakeJobRun(
        "run-stale",
        "library_refresh",
        "sub-a",
        "rate_limited",
        updated_at=datetime.now(timezone.utc) - timedelta(hours=25),
    )
    job_runs_repository = FakeJobRunsRepository([stale])
    client, validator, publisher = _build(job_runs_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/library/refresh", headers=_bearer("token-a"))

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-1"}
    assert publisher.library_refresh_calls == ["sub-a"]
    assert job_runs_repository.failed_calls == [
        ("run-stale", "Superseded: no progress for over 24 hours, treated as abandoned.")
    ]
    assert job_runs_repository.runs["run-stale"].status == "failed"


def test_a_run_within_the_staleness_threshold_is_not_superseded_even_while_rate_limited():

    waiting = FakeJobRun(
        "run-waiting",
        "library_refresh",
        "sub-a",
        "rate_limited",
        updated_at=datetime.now(timezone.utc) - timedelta(hours=8),
    )
    job_runs_repository = FakeJobRunsRepository([waiting])
    client, validator, publisher = _build(job_runs_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post("/library/refresh", headers=_bearer("token-a"))

    assert response.status_code == 202
    assert response.json() == {"run_id": "run-waiting"}
    assert publisher.library_refresh_calls == []
    assert job_runs_repository.failed_calls == []


def test_queue_not_configured_returns_503():
    client, validator, _publisher = _build()
    client.app.state.queue_publisher = None
    validator.register("token-a", _claims())

    response = client.post("/library/refresh", headers=_bearer("token-a"))

    assert response.status_code == 503


def test_get_status_returns_run_for_owner():
    run = FakeJobRun("run-1", "library_refresh", "sub-a", "running")
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library/refresh/run-1", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == {"run_id": "run-1", "status": "running", "error": None, "result_summary": None}


def test_get_status_returns_result_summary_when_present():
    summary = {"rawg_enriched_titles": ["Elden Ring"], "opencritic_topup_incomplete": False}
    run = FakeJobRun("run-1", "library_refresh", "sub-a", "succeeded", result_summary=summary)
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library/refresh/run-1", headers=_bearer("token-a"))

    assert response.json()["result_summary"] == summary


def test_get_status_unknown_run_returns_404():
    client, validator, _publisher = _build()
    validator.register("token-a", _claims())

    response = client.get("/library/refresh/unknown", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_get_status_not_owned_returns_404():
    run = FakeJobRun("run-1", "library_refresh", "sub-b", "succeeded")
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library/refresh/run-1", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_get_status_enrichment_run_returns_404():
    run = FakeJobRun("run-1", "enrichment", None, "succeeded")
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims())

    response = client.get("/library/refresh/run-1", headers=_bearer("token-a"))

    assert response.status_code == 404


def test_get_library_requires_bearer_token():
    client, _validator, _publisher = _build()

    assert client.get("/library").status_code == 401


def test_get_library_returns_callers_own_games_with_ratings_and_category():
    games = [
        FakeLibraryGameView(
            "game-1",
            "Elden Ring",
            category="Action RPG",
            rawg_rating=96.0,
            opencritic_rating=94.0,
            psn_rating=4.8,
            psn_product_id="UP0700-CUSA23100_00-ELDENRING0000000",
            rawg_enriched=True,
            opencritic_enriched=True,
            cover_image_url="https://cdn.example/elden-ring.jpg",
            platforms=("PS5", "PS4"),
        ),
        FakeLibraryGameView("game-2", "Unmatched Game", rawg_enriched=False, opencritic_enriched=False),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == {
        "games": [
            {
                "game_id": "game-1",
                "title": "Elden Ring",
                "category": "Action RPG",
                "rawg_rating": 96.0,
                "opencritic_rating": 94.0,
                "psn_rating": 4.8,
                "psn_product_id": "UP0700-CUSA23100_00-ELDENRING0000000",
                "rawg_enriched": True,
                "opencritic_enriched": True,
                "is_active": True,
                "percent_completed": None,
                "source": "psn",
                "cover_image_url": "https://cdn.example/elden-ring.jpg",
                "platforms": ["PS5", "PS4"],
            },
            {
                "game_id": "game-2",
                "title": "Unmatched Game",
                "category": None,
                "rawg_rating": None,
                "opencritic_rating": None,
                "psn_rating": None,
                "psn_product_id": None,
                "rawg_enriched": False,
                "opencritic_enriched": False,
                "is_active": True,
                "percent_completed": None,
                "source": "psn",
                "cover_image_url": None,
                "platforms": [],
            },
        ],
        "total": 2,
    }


def test_get_library_preserves_platform_order_from_the_repository():
    """Platforms arrive ordered newest-first by ``platforms.sort_order``; the route must not re-sort
    them into alphabetical order, which would read PS3 before PS5."""
    games = [FakeLibraryGameView("game-1", "99Vidas", platforms=("PS4", "PS3", "PSVITA"))]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json()["games"][0]["platforms"] == ["PS4", "PS3", "PSVITA"]


def test_get_library_flags_a_game_the_caller_lost_access_to():

    games = [
        FakeLibraryGameView("game-1", "Still Mine"),
        FakeLibraryGameView("game-2", "Lapsed Plus Title", is_active=False),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library", headers=_bearer("token-a"))

    assert response.status_code == 200
    by_title = {game["title"]: game["is_active"] for game in response.json()["games"]}
    assert by_title == {"Still Mine": True, "Lapsed Plus Title": False}


def test_get_library_returns_empty_page_for_a_user_with_no_entries():
    client, validator, _publisher = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == {"games": [], "total": 0}


def test_get_library_scoped_to_caller_only():
    games_a = [FakeLibraryGameView("game-1", "A's Game")]
    games_b = [FakeLibraryGameView("game-2", "B's Game")]
    client, validator, _publisher = _build(
        library_repository=FakeLibraryRepository({"sub-a": games_a, "sub-b": games_b})
    )
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library", headers=_bearer("token-a"))

    assert [game["title"] for game in response.json()["games"]] == ["A's Game"]


def test_get_library_search_filters_by_title_substring_case_insensitively():
    games = [FakeLibraryGameView("game-1", "Elden Ring"), FakeLibraryGameView("game-2", "Bloodborne")]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library?q=elden", headers=_bearer("token-a"))

    body = response.json()
    assert [g["title"] for g in body["games"]] == ["Elden Ring"]
    assert body["total"] == 1


def test_get_library_category_filters_exact_match():
    games = [
        FakeLibraryGameView("game-1", "Elden Ring", category="Action RPG"),
        FakeLibraryGameView("game-2", "Tetris Effect", category="Puzzle"),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library?category=Puzzle", headers=_bearer("token-a"))

    body = response.json()
    assert [g["title"] for g in body["games"]] == ["Tetris Effect"]
    assert body["total"] == 1


def test_get_library_sort_by_rating_nulls_last_ascending_and_descending():
    games = [
        FakeLibraryGameView("g1", "No Rating"),
        FakeLibraryGameView("g2", "High", rawg_rating=90.0),
        FakeLibraryGameView("g3", "Low", rawg_rating=40.0),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    asc = client.get("/library?sort=rawg_rating&sortDir=asc", headers=_bearer("token-a")).json()
    assert [g["title"] for g in asc["games"]] == ["Low", "High", "No Rating"]

    desc = client.get("/library?sort=rawg_rating&sortDir=desc", headers=_bearer("token-a")).json()
    assert [g["title"] for g in desc["games"]] == ["High", "Low", "No Rating"]


def test_get_library_sort_by_percent_completed_nulls_last_ascending_and_descending():
    games = [
        FakeLibraryGameView("g1", "No Progress"),
        FakeLibraryGameView("g2", "Mostly Done", percent_completed=90),
        FakeLibraryGameView("g3", "Barely Started", percent_completed=10),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    asc = client.get("/library?sort=percent_completed&sortDir=asc", headers=_bearer("token-a")).json()
    assert [g["title"] for g in asc["games"]] == ["Barely Started", "Mostly Done", "No Progress"]

    desc = client.get("/library?sort=percent_completed&sortDir=desc", headers=_bearer("token-a")).json()
    assert [g["title"] for g in desc["games"]] == ["Mostly Done", "Barely Started", "No Progress"]


def test_get_library_rejects_unknown_sort_field():
    client, validator, _publisher = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library?sort=not_a_real_field", headers=_bearer("token-a"))

    assert response.status_code == 422


def test_get_library_pagination_limit_and_offset():
    games = [FakeLibraryGameView(f"g{i}", f"Game {i}") for i in range(5)]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library?limit=2&offset=2", headers=_bearer("token-a"))

    body = response.json()
    assert [g["title"] for g in body["games"]] == ["Game 2", "Game 3"]
    assert body["total"] == 5


def test_get_library_categories_returns_distinct_sorted_categories():
    games = [
        FakeLibraryGameView("g1", "A", category="RPG"),
        FakeLibraryGameView("g2", "B", category="Puzzle"),
        FakeLibraryGameView("g3", "C", category="RPG"),
        FakeLibraryGameView("g4", "D", category=None),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library/categories", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == {"categories": ["Puzzle", "RPG"]}


def test_get_library_categories_empty_for_user_with_no_categorized_games():
    client, validator, _publisher = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library/categories", headers=_bearer("token-a"))

    assert response.status_code == 200
    assert response.json() == {"categories": []}


def test_get_library_percent_completed_comes_from_the_stored_column():
    games = [FakeLibraryGameView("game-1", "Game A", percent_completed=50)]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library", headers=_bearer("token-a"))

    assert response.json()["games"][0]["percent_completed"] == 50


def test_get_library_percent_completed_blank_for_unlinked_user():
    games = [FakeLibraryGameView("game-1", "God of War Ragnarök")]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library", headers=_bearer("token-a"))

    assert response.json()["games"][0]["percent_completed"] is None


def test_get_library_percent_completed_blank_when_harvest_trophies_disabled():
    games = [FakeLibraryGameView("game-1", "God of War Ragnarök")]
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, "sub-a", harvest_trophies=False)
    client, validator, _publisher = _build(
        library_repository=FakeLibraryRepository({"sub-a": games}), repository=repository
    )
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library", headers=_bearer("token-a"))

    assert response.json()["games"][0]["percent_completed"] is None


def test_get_library_never_calls_psn_to_resolve_completion():
    """Rendering the library must not depend on PSN being reachable.

    Every game here has a stored percentage, and the caller is linked with harvesting enabled -- yet no
    trophy client is built. Before ``0015_library_entries_trophy_progress.sql`` this path fuzzy-matched
    the page's titles against a live ``trophy_titles()`` fetch on every request, so a stale token or a
    cold Redis silently blanked the column.
    """
    games = [
        FakeLibraryGameView("game-1", "Game A", percent_completed=63),
        FakeLibraryGameView("game-2", "Game B", percent_completed=None),
    ]
    repository = FakeRepository()
    crypto = TokenCrypto(Fernet.generate_key())
    _seed_link(repository, crypto, "sub-a", harvest_trophies=True)
    factory = FakeTrophyClientFactory()
    factory.linked["sub-a"] = FakeTrophyClient()
    client, validator, _publisher = _build(
        library_repository=FakeLibraryRepository({"sub-a": games}),
        repository=repository,
        trophy_client_factory=factory,
    )
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get("/library", headers=_bearer("token-a"))

    by_title = {game["title"]: game["percent_completed"] for game in response.json()["games"]}
    assert by_title == {"Game A": 63, "Game B": None}
    assert factory.calls == []
