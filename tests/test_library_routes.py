"""Tests for POST /library/refresh, using create_app() with a fake QueuePublisher."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from audit_fakes import RecordingAuditRepository
from curator import library_routes
from curator.app import create_app
from curator.audit.repository import ACTION_LIBRARY_REFRESH_REQUESTED
from curator.jobs.repository import (
    JOB_KIND_ENRICHMENT,
    JOB_KIND_LIBRARY_REFRESH,
    JOB_STATUS_FAILED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    TERMINAL_STATUSES,
)
from curator.jobs.staleness import lease_lapsed_reason, no_progress_reason
from curator.library.repository import (
    HIDDEN_EXCLUDE,
    HIDDEN_ONLY,
    LIBRARY_SOURCE_PSN,
    TROPHY_MATCHED,
    TROPHY_NOT_ATTEMPTED,
    TROPHY_UNMATCHED,
)
from curator.library_routes import (
    LIBRARY_REFRESH_RUN_NOUN,
    TROPHY_PROGRESS_HARVEST_OFF,
    TROPHY_PROGRESS_NEVER_REFRESHED,
    TROPHY_PROGRESS_NO_LINK,
    TROPHY_PROGRESS_OFF,
    TROPHY_PROGRESS_ON,
    TROPHY_PROGRESS_PENDING,
    LibraryGameResponse,
    LibraryGenresResponse,
    LibraryPageResponse,
    LibraryRefreshResponse,
    LibraryRefreshStatusResponse,
    ManualGameRequest,
    TrophyProgressResponse,
)
from curator.persistence.crypto import TokenCrypto
from curator.psn.title_platform import PS3, PS4, PS5, PSVITA, platform_vocabulary_message
from test_routes import (
    FakeAgentFactory,
    FakeRepository,
    FakeTokenValidator,
    _bearer,
    _claims,
    _make_settings,
    _path,
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
    def __init__(
        self,
        run_id,
        kind,
        identity_sub,
        status,
        error=None,
        result_summary=None,
        updated_at=None,
        lease_expires_at=None,
    ):
        self.run_id = run_id
        self.kind = kind
        self.identity_sub = identity_sub
        self.status = status
        self.error = error
        self.result_summary = result_summary
        self.updated_at = updated_at if updated_at is not None else datetime.now(timezone.utc)
        self.lease_expires_at = lease_expires_at


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
            if run.identity_sub == identity_sub and run.kind == kind and run.status not in TERMINAL_STATUSES
        ]
        return max(candidates, key=lambda run: run.updated_at, default=None)

    async def mark_failed(self, run_id, error):
        self.failed_calls.append((run_id, error))
        self.runs[run_id].status = JOB_STATUS_FAILED
        self.runs[run_id].error = error


class FakeLibraryGameView:
    def __init__(
        self,
        game_id,
        title,
        genre=None,
        rawg_rating=None,
        opencritic_rating=None,
        psn_rating=None,
        psn_product_id=None,
        rawg_enriched=False,
        opencritic_enriched=False,
        psn_enriched=False,
        is_active=True,
        np_communication_id=None,
        percent_completed=None,
        source=LIBRARY_SOURCE_PSN,
        cover_image_url=None,
        platforms=(),
        trophy_match=TROPHY_NOT_ATTEMPTED,
    ):
        self.game_id = game_id
        self.title = title
        self.genre = genre
        self.rawg_rating = rawg_rating
        self.opencritic_rating = opencritic_rating
        self.psn_rating = psn_rating
        self.psn_product_id = psn_product_id
        self.rawg_enriched = rawg_enriched
        self.opencritic_enriched = opencritic_enriched
        self.psn_enriched = psn_enriched
        self.is_active = is_active
        self.np_communication_id = np_communication_id
        self.percent_completed = percent_completed
        self.source = source
        self.cover_image_url = cover_image_url
        self.platforms = platforms
        self.trophy_match = trophy_match


class FakeLibraryRepository:
    """Route-level stand-in that reimplements search/genre/sort/paging in memory so the route's own
    parameter plumbing can be exercised. It proves nothing about the SQL: the production predicates live
    in ``curator.library.repository`` and are asserted on their query text in
    ``tests/test_library_repository.py`` (ILIKE, ``gen.name = %s``, and the NULLS LAST ordering
    on both sortable enrichment columns)."""

    def __init__(
        self,
        games_by_sub=None,
        manual_upsert_writes=True,
        manual_rows=(),
        psn_rows=(),
        *,
        has_trophy_progress=False,
        hidden_game_ids=(),
    ):
        self._games_by_sub = games_by_sub or {}
        self.manual_entries: list[tuple[str, str, tuple[str, ...], str | None]] = []
        self.manual_upsert_writes = manual_upsert_writes
        self.manual_rows = {tuple(row) for row in manual_rows}
        self.psn_rows = {tuple(row) for row in psn_rows}
        self._has_trophy_progress = has_trophy_progress
        self.hidden: set[tuple[str, str]] = set(hidden_game_ids)
        self.hidden_filters: list[str] = []

    async def has_trophy_progress(self, identity_sub):
        return self._has_trophy_progress

    async def hide_entry(self, identity_sub, game_id):
        held = any(g.game_id == game_id for g in self._games_by_sub.get(identity_sub, []))
        if held:
            self.hidden.add((identity_sub, game_id))
        return held

    async def unhide_entry(self, identity_sub, game_id):
        self.hidden.discard((identity_sub, game_id))

    async def count_hidden(self, identity_sub):
        return sum(1 for sub, _game in self.hidden if sub == identity_sub)

    async def list_entries_with_enrichment(
        self,
        identity_sub,
        *,
        search=None,
        genre=None,
        sort="title",
        sort_dir="asc",
        limit=20,
        offset=0,
        hidden=HIDDEN_EXCLUDE,
    ):
        self.hidden_filters.append(hidden)
        games = list(self._games_by_sub.get(identity_sub, []))
        hidden_ids = {game for sub, game in self.hidden if sub == identity_sub}
        games = [g for g in games if (g.game_id in hidden_ids) == (hidden == HIDDEN_ONLY)]
        if search:
            games = [g for g in games if search.lower() in g.title.lower()]
        if genre:
            games = [g for g in games if g.genre == genre]

        attr = sort
        reverse = sort_dir == "desc"
        games.sort(key=lambda g: (getattr(g, attr) is None, getattr(g, attr), g.title), reverse=False)
        if reverse:
            non_null = [g for g in games if getattr(g, attr) is not None]
            non_null.sort(key=lambda g: getattr(g, attr), reverse=True)
            null = [g for g in games if getattr(g, attr) is None]
            games = non_null + null

        total = len(games)
        return games[offset : offset + limit], total

    async def list_genres(self, identity_sub):
        games = self._games_by_sub.get(identity_sub, [])
        return sorted({g.genre for g in games if g.genre is not None})

    async def upsert_manual_entry(self, identity_sub, game_id, *, platforms, owned_edition):
        self.manual_entries.append((identity_sub, game_id, tuple(platforms), owned_edition))
        return self.manual_upsert_writes

    async def delete_manual_entry(self, identity_sub, game_id):
        """Mirrors the production predicate's reach: the row goes only when it is the caller's own AND
        carries ``source = 'manual'``. A PSN-sourced row and an absent one are indistinguishable to the
        caller, which is why both leave ``psn_rows`` untouched and report the same ``False``."""
        row = (identity_sub, game_id)
        if row not in self.manual_rows:
            return False
        self.manual_rows.remove(row)
        return True


class FakeCatalogRepository:
    def __init__(self, known_games=(), title_ids_by_game=None):
        self._known_games = set(known_games)
        self._title_ids_by_game = title_ids_by_game or {}

    async def game_exists(self, game_id):
        return game_id in self._known_games

    async def title_id_for_game(self, game_id):
        return self._title_ids_by_game.get(game_id)


def _build(
    job_runs_repository=None,
    library_repository=None,
    repository=None,
    trophy_client_factory=None,
    catalog_repository=None,
):
    repository = repository if repository is not None else FakeRepository()
    token_crypto = TokenCrypto(TokenCrypto.generate_key())
    validator = FakeTokenValidator()
    publisher = FakePublisher()
    app = create_app(
        _make_settings(),
        repository=repository,
        token_crypto=token_crypto,
        agent_factory=FakeAgentFactory(repository, token_crypto),
        token_validator=validator,
        trophy_client_factory=trophy_client_factory or FakeTrophyClientFactory(),
        audit_repository=RecordingAuditRepository(),
    )
    app.state.queue_publisher = publisher
    app.state.job_runs_repository = job_runs_repository or FakeJobRunsRepository()
    app.state.library_repository = library_repository or FakeLibraryRepository()
    if catalog_repository is not None:
        app.state.catalog_repository = catalog_repository
    return TestClient(app), validator, publisher


def _build_manual(known_games=("game-1",), title_ids_by_game=None, manual_upsert_writes=True):
    library = FakeLibraryRepository(manual_upsert_writes=manual_upsert_writes)
    client, validator, _publisher = _build(
        library_repository=library,
        catalog_repository=FakeCatalogRepository(known_games, title_ids_by_game),
    )
    validator.register("token-a", _claims(sub="sub-a"))
    return client, library


def test_add_manual_game_records_every_platform_the_caller_named():
    client, library = _build_manual()

    response = client.post(
        _path(client, library_routes.add_manual_game),
        json=ManualGameRequest(game_id="game-1", platforms=[PS3, PSVITA]).model_dump(),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 204
    assert library.manual_entries == [("sub-a", "game-1", (PS3, PSVITA), None)]


def test_add_manual_game_still_accepts_the_deprecated_boolean_pair():
    """Librarian sends native_ps5/ps4_eligible today, so dropping them would break the running client."""
    client, library = _build_manual()

    response = client.post(
        _path(client, library_routes.add_manual_game),
        json=ManualGameRequest(game_id="game-1", native_ps5=True, ps4_eligible=True).model_dump(),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 204
    assert library.manual_entries[0][2] == (PS5, PS4)


def test_add_manual_game_does_not_duplicate_a_platform_named_both_ways():
    client, library = _build_manual()

    client.post(
        _path(client, library_routes.add_manual_game),
        json=ManualGameRequest(game_id="game-1", platforms=[PS5], native_ps5=True).model_dump(),
        headers=_bearer("token-a"),
    )

    assert library.manual_entries[0][2] == (PS5,)


def test_add_manual_game_derives_the_platform_from_the_catalogs_own_title_id():
    """A PS3 disc has no boolean to set and the client has no reason to know PSN's prefix table."""
    client, library = _build_manual(title_ids_by_game={"game-1": "BLUS30443_00"})

    response = client.post(
        _path(client, library_routes.add_manual_game),
        json=ManualGameRequest(game_id="game-1").model_dump(),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 204
    assert library.manual_entries[0][2] == (PS3,)


def test_a_caller_supplied_platform_wins_over_the_derived_one():
    client, library = _build_manual(title_ids_by_game={"game-1": "BLUS30443_00"})

    client.post(
        _path(client, library_routes.add_manual_game),
        json=ManualGameRequest(game_id="game-1", platforms=[PS5]).model_dump(),
        headers=_bearer("token-a"),
    )

    assert library.manual_entries[0][2] == (PS5,)


def test_add_manual_game_records_no_platform_when_the_prefix_is_not_a_title():
    """NPIA is PS Plus SKUs and their reward children, not a PS3 prefix -- deriving one would put a
    discount coupon's platform on a real game."""
    client, library = _build_manual(title_ids_by_game={"game-1": "NPIA90007_01"})

    client.post(
        _path(client, library_routes.add_manual_game),
        json=ManualGameRequest(game_id="game-1").model_dump(),
        headers=_bearer("token-a"),
    )

    assert library.manual_entries[0][2] == ()


def test_adding_a_game_the_caller_already_holds_from_psn_says_so_instead_of_reporting_success():
    """The ON CONFLICT guard declines to touch a PSN-sourced row, and answering 204 anyway told the user
    their game had been added. A lapsed entitlement is exactly this shape: the row is inactive and carries
    no art or scores, so the game reads as absent right until the add quietly does nothing."""
    client, _library = _build_manual(manual_upsert_writes=False)

    response = client.post(
        _path(client, library_routes.add_manual_game),
        json=ManualGameRequest(game_id="game-1").model_dump(),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 409
    assert "already in your library" in response.json()["detail"]


def test_add_manual_game_rejects_a_platform_outside_the_vocabulary():
    client, library = _build_manual()

    response = client.post(
        _path(client, library_routes.add_manual_game),
        json=ManualGameRequest(game_id="game-1", platforms=[uuid.uuid4().hex]).model_dump(),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 400
    assert response.json()["detail"] == platform_vocabulary_message()
    assert library.manual_entries == []


def test_add_manual_game_404s_for_a_game_the_catalog_has_never_seen():
    client, library = _build_manual(known_games=())

    response = client.post(
        _path(client, library_routes.add_manual_game),
        json=ManualGameRequest(game_id="game-1").model_dump(),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 404
    assert library.manual_entries == []


def _build_removable(manual_rows=(), psn_rows=()):
    library = FakeLibraryRepository(manual_rows=manual_rows, psn_rows=psn_rows)
    client, validator, _publisher = _build(library_repository=library)
    validator.register("token-a", _claims(sub="sub-a"))
    return client, library


def test_removing_a_manually_added_game_deletes_it():
    client, library = _build_removable(manual_rows=[("sub-a", "game-1")])

    response = client.delete(
        _path(client, library_routes.remove_manual_game, game_id="game-1"), headers=_bearer("token-a")
    )

    assert response.status_code == 204
    assert library.manual_rows == set()


def test_removing_a_psn_sourced_game_is_refused_rather_than_deleting_it():
    """A PSN entitlement is re-ingested by the next library refresh, so honouring the delete would remove
    the row until the refresh silently put it back. Only a manually-added row is the caller's to remove --
    the guard is the ``source = 'manual'`` predicate, and this is the only test that exercises it through
    the route rather than by reading the query text."""
    client, library = _build_removable(psn_rows=[("sub-a", "game-1")])

    response = client.delete(
        _path(client, library_routes.remove_manual_game, game_id="game-1"), headers=_bearer("token-a")
    )

    assert response.status_code == 404
    assert library.psn_rows == {("sub-a", "game-1")}


def test_removing_another_callers_manual_game_leaves_it_alone():
    """The route keys off the token's own sub, so naming someone else's game reads as absent."""
    client, library = _build_removable(manual_rows=[("sub-b", "game-1")])

    response = client.delete(
        _path(client, library_routes.remove_manual_game, game_id="game-1"), headers=_bearer("token-a")
    )

    assert response.status_code == 404
    assert library.manual_rows == {("sub-b", "game-1")}


def test_an_unknown_game_reports_404_even_when_the_platform_is_also_wrong():
    """Which error a caller sees must not depend on validation order, so the resource check stays first."""
    client, library = _build_manual(known_games=())

    response = client.post(
        _path(client, library_routes.add_manual_game),
        json=ManualGameRequest(game_id="game-1", platforms=[uuid.uuid4().hex]).model_dump(),
        headers=_bearer("token-a"),
    )

    assert response.status_code == 404
    assert library.manual_entries == []


def test_requires_bearer_token():
    client, _validator, _publisher = _build()

    response = client.post(_path(client, library_routes.refresh_library))

    assert response.status_code == 401


def test_publishes_for_the_callers_own_sub_and_returns_run_id():
    client, validator, publisher = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(_path(client, library_routes.refresh_library), headers=_bearer("token-a"))

    assert response.status_code == 202
    assert LibraryRefreshResponse.model_validate(response.json()).run_id == "run-1"
    assert publisher.library_refresh_calls == ["sub-a"]
    assert client.app.state.audit_repository.entries == [("sub-a", ACTION_LIBRARY_REFRESH_REQUESTED, "run-1")]


def test_a_refresh_is_not_queued_when_its_history_row_cannot_be_written():
    client, validator, publisher = _build()
    validator.register("token-a", _claims(sub="sub-a"))
    client.app.state.audit_repository.begin_error = RuntimeError("sub-a")

    with pytest.raises(RuntimeError):
        client.post(_path(client, library_routes.refresh_library), headers=_bearer("token-a"))

    assert publisher.library_refresh_calls == []


def test_duplicate_refresh_returns_existing_run_id_instead_of_publishing_again():
    active = FakeJobRun(
        "run-existing",
        JOB_KIND_LIBRARY_REFRESH,
        "sub-a",
        JOB_STATUS_RUNNING,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    client, validator, publisher = _build(FakeJobRunsRepository([active]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(_path(client, library_routes.refresh_library), headers=_bearer("token-a"))

    assert response.status_code == 202
    assert LibraryRefreshResponse.model_validate(response.json()).run_id == "run-existing"
    assert publisher.library_refresh_calls == []


def test_duplicate_refresh_guard_is_scoped_to_the_caller_and_kind():
    other_users_run = FakeJobRun("run-other", JOB_KIND_LIBRARY_REFRESH, "sub-b", JOB_STATUS_RUNNING)
    enrichment_run = FakeJobRun("run-enrichment", JOB_KIND_ENRICHMENT, None, JOB_STATUS_RUNNING)
    client, validator, publisher = _build(FakeJobRunsRepository([other_users_run, enrichment_run]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(_path(client, library_routes.refresh_library), headers=_bearer("token-a"))

    assert response.status_code == 202
    assert LibraryRefreshResponse.model_validate(response.json()).run_id == "run-1"
    assert publisher.library_refresh_calls == ["sub-a"]


def test_a_terminal_run_does_not_block_a_new_refresh():
    finished = FakeJobRun("run-old", JOB_KIND_LIBRARY_REFRESH, "sub-a", JOB_STATUS_SUCCEEDED)
    client, validator, publisher = _build(FakeJobRunsRepository([finished]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(_path(client, library_routes.refresh_library), headers=_bearer("token-a"))

    assert response.status_code == 202
    assert LibraryRefreshResponse.model_validate(response.json()).run_id == "run-1"
    assert publisher.library_refresh_calls == ["sub-a"]


def test_a_stale_non_terminal_run_is_superseded_not_returned():

    stale = FakeJobRun(
        "run-stale",
        JOB_KIND_LIBRARY_REFRESH,
        "sub-a",
        "rate_limited",
        updated_at=datetime.now(timezone.utc) - timedelta(hours=25),
    )
    job_runs_repository = FakeJobRunsRepository([stale])
    client, validator, publisher = _build(job_runs_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(_path(client, library_routes.refresh_library), headers=_bearer("token-a"))

    assert response.status_code == 202
    assert LibraryRefreshResponse.model_validate(response.json()).run_id == "run-1"
    assert publisher.library_refresh_calls == ["sub-a"]
    assert job_runs_repository.failed_calls == [("run-stale", no_progress_reason(LIBRARY_REFRESH_RUN_NOUN))]
    assert job_runs_repository.runs["run-stale"].status == JOB_STATUS_FAILED


def test_a_running_run_holding_a_live_lease_is_returned_not_superseded():
    alive = FakeJobRun(
        "run-alive",
        JOB_KIND_LIBRARY_REFRESH,
        "sub-a",
        JOB_STATUS_RUNNING,
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=2),
    )
    job_runs_repository = FakeJobRunsRepository([alive])
    client, validator, publisher = _build(job_runs_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(_path(client, library_routes.refresh_library), headers=_bearer("token-a"))

    assert LibraryRefreshResponse.model_validate(response.json()).run_id == "run-alive"
    assert publisher.library_refresh_calls == []
    assert job_runs_repository.failed_calls == []


def test_a_running_run_whose_lease_expired_is_superseded_even_though_updated_at_is_recent():
    dead = FakeJobRun(
        "run-dead",
        JOB_KIND_LIBRARY_REFRESH,
        "sub-a",
        JOB_STATUS_RUNNING,
        updated_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        lease_expires_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    job_runs_repository = FakeJobRunsRepository([dead])
    client, validator, publisher = _build(job_runs_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(_path(client, library_routes.refresh_library), headers=_bearer("token-a"))

    assert LibraryRefreshResponse.model_validate(response.json()).run_id == "run-1"
    assert publisher.library_refresh_calls == ["sub-a"]
    assert job_runs_repository.failed_calls == [("run-dead", lease_lapsed_reason(LIBRARY_REFRESH_RUN_NOUN))]


def test_a_running_run_that_never_took_a_lease_is_superseded():
    unleased = FakeJobRun("run-unleased", JOB_KIND_LIBRARY_REFRESH, "sub-a", JOB_STATUS_RUNNING, lease_expires_at=None)
    job_runs_repository = FakeJobRunsRepository([unleased])
    client, validator, _publisher = _build(job_runs_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(_path(client, library_routes.refresh_library), headers=_bearer("token-a"))

    assert LibraryRefreshResponse.model_validate(response.json()).run_id == "run-1"
    assert job_runs_repository.failed_calls == [("run-unleased", lease_lapsed_reason(LIBRARY_REFRESH_RUN_NOUN))]


def test_a_run_within_the_staleness_threshold_is_not_superseded_even_while_rate_limited():

    waiting = FakeJobRun(
        "run-waiting",
        JOB_KIND_LIBRARY_REFRESH,
        "sub-a",
        "rate_limited",
        updated_at=datetime.now(timezone.utc) - timedelta(hours=8),
    )
    job_runs_repository = FakeJobRunsRepository([waiting])
    client, validator, publisher = _build(job_runs_repository)
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.post(_path(client, library_routes.refresh_library), headers=_bearer("token-a"))

    assert response.status_code == 202
    assert LibraryRefreshResponse.model_validate(response.json()).run_id == "run-waiting"
    assert publisher.library_refresh_calls == []
    assert job_runs_repository.failed_calls == []


def test_queue_not_configured_returns_503():
    client, validator, _publisher = _build()
    client.app.state.queue_publisher = None
    validator.register("token-a", _claims())

    response = client.post(_path(client, library_routes.refresh_library), headers=_bearer("token-a"))

    assert response.status_code == 503


def test_get_status_returns_run_for_owner():
    run = FakeJobRun("run-1", JOB_KIND_LIBRARY_REFRESH, "sub-a", JOB_STATUS_RUNNING)
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(
        _path(client, library_routes.get_library_refresh_status, run_id="run-1"), headers=_bearer("token-a")
    )

    assert response.status_code == 200
    assert LibraryRefreshStatusResponse.model_validate(response.json()) == LibraryRefreshStatusResponse(
        run_id="run-1", status=JOB_STATUS_RUNNING, error=None, result_summary=None
    )


def test_get_status_returns_result_summary_when_present():
    summary = {"rawg_enriched_titles": ["Elden Ring"], "opencritic_topup_incomplete": False}
    run = FakeJobRun("run-1", JOB_KIND_LIBRARY_REFRESH, "sub-a", JOB_STATUS_SUCCEEDED, result_summary=summary)
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(
        _path(client, library_routes.get_library_refresh_status, run_id="run-1"), headers=_bearer("token-a")
    )

    assert LibraryRefreshStatusResponse.model_validate(response.json()).result_summary == summary


def test_get_status_unknown_run_returns_404():
    client, validator, _publisher = _build()
    validator.register("token-a", _claims())

    response = client.get(
        _path(client, library_routes.get_library_refresh_status, run_id="unknown"), headers=_bearer("token-a")
    )

    assert response.status_code == 404


def test_get_status_not_owned_returns_404():
    run = FakeJobRun("run-1", JOB_KIND_LIBRARY_REFRESH, "sub-b", JOB_STATUS_SUCCEEDED)
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(
        _path(client, library_routes.get_library_refresh_status, run_id="run-1"), headers=_bearer("token-a")
    )

    assert response.status_code == 404


def test_get_status_enrichment_run_returns_404():
    run = FakeJobRun("run-1", JOB_KIND_ENRICHMENT, None, JOB_STATUS_SUCCEEDED)
    client, validator, _publisher = _build(FakeJobRunsRepository([run]))
    validator.register("token-a", _claims())

    response = client.get(
        _path(client, library_routes.get_library_refresh_status, run_id="run-1"), headers=_bearer("token-a")
    )

    assert response.status_code == 404


def test_get_library_reports_trophy_progress_off_for_an_unlinked_caller():
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": []}))
    validator.register("token-a", _claims(sub="sub-a"))

    body = LibraryPageResponse.model_validate(
        client.get(_path(client, library_routes.get_library), headers=_bearer("token-a")).json()
    )

    assert body.trophy_progress == TrophyProgressResponse(state=TROPHY_PROGRESS_OFF, reason=TROPHY_PROGRESS_NO_LINK)


def test_get_library_reports_trophy_progress_off_when_harvesting_is_disabled():
    repository = FakeRepository()
    _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), "sub-a", harvest_trophies=False)
    client, validator, _publisher = _build(
        library_repository=FakeLibraryRepository({"sub-a": []}), repository=repository
    )
    validator.register("token-a", _claims(sub="sub-a"))

    body = LibraryPageResponse.model_validate(
        client.get(_path(client, library_routes.get_library), headers=_bearer("token-a")).json()
    )

    assert body.trophy_progress == TrophyProgressResponse(state=TROPHY_PROGRESS_OFF, reason=TROPHY_PROGRESS_HARVEST_OFF)


def test_get_library_withholds_a_stored_percentage_while_harvesting_is_off():
    repository = FakeRepository()
    _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), "sub-a", harvest_trophies=False)
    stored_percent = uuid.uuid4().int % 99 + 1
    games = {"sub-a": [FakeLibraryGameView("g1", "Abzu", percent_completed=stored_percent)]}
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository(games), repository=repository)
    validator.register("token-a", _claims(sub="sub-a"))

    body = LibraryPageResponse.model_validate(
        client.get(_path(client, library_routes.get_library), headers=_bearer("token-a")).json()
    )

    assert body.trophy_progress.state == TROPHY_PROGRESS_OFF
    assert body.games[0].percent_completed is None


def test_get_library_serves_a_stored_percentage_while_harvesting_is_on():
    repository = FakeRepository()
    _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), "sub-a", harvest_trophies=True)
    stored_percent = uuid.uuid4().int % 99 + 1
    games = {"sub-a": [FakeLibraryGameView("g1", "Abzu", percent_completed=stored_percent)]}
    client, validator, _publisher = _build(
        library_repository=FakeLibraryRepository(games, has_trophy_progress=True), repository=repository
    )
    validator.register("token-a", _claims(sub="sub-a"))

    body = LibraryPageResponse.model_validate(
        client.get(_path(client, library_routes.get_library), headers=_bearer("token-a")).json()
    )

    assert body.trophy_progress.state == TROPHY_PROGRESS_ON
    assert body.games[0].percent_completed == stored_percent


def test_get_library_reports_trophy_progress_pending_until_a_refresh_has_fetched_any():
    repository = FakeRepository()
    _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), "sub-a", harvest_trophies=True)
    client, validator, _publisher = _build(
        library_repository=FakeLibraryRepository({"sub-a": []}, has_trophy_progress=False), repository=repository
    )
    validator.register("token-a", _claims(sub="sub-a"))

    body = LibraryPageResponse.model_validate(
        client.get(_path(client, library_routes.get_library), headers=_bearer("token-a")).json()
    )

    assert body.trophy_progress == TrophyProgressResponse(
        state=TROPHY_PROGRESS_PENDING, reason=TROPHY_PROGRESS_NEVER_REFRESHED
    )


def test_get_library_reports_trophy_progress_on_once_a_refresh_has_fetched_progress():
    repository = FakeRepository()
    _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), "sub-a", harvest_trophies=True)
    client, validator, _publisher = _build(
        library_repository=FakeLibraryRepository({"sub-a": []}, has_trophy_progress=True), repository=repository
    )
    validator.register("token-a", _claims(sub="sub-a"))

    body = LibraryPageResponse.model_validate(
        client.get(_path(client, library_routes.get_library), headers=_bearer("token-a")).json()
    )

    assert body.trophy_progress == TrophyProgressResponse(state=TROPHY_PROGRESS_ON, reason=None)


def test_get_library_carries_each_rows_trophy_match_state():
    games = [
        FakeLibraryGameView("g1", "Matched", trophy_match=TROPHY_MATCHED),
        FakeLibraryGameView("g2", "Unmatched", trophy_match=TROPHY_UNMATCHED),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    body = LibraryPageResponse.model_validate(
        client.get(_path(client, library_routes.get_library), headers=_bearer("token-a")).json()
    )

    assert [row.trophy_match for row in body.games] == [TROPHY_MATCHED, TROPHY_UNMATCHED]


def test_hiding_a_held_game_removes_it_from_the_library_and_counts_it():
    games = [FakeLibraryGameView("g1", "Keep"), FakeLibraryGameView("g2", "Hide me")]
    library = FakeLibraryRepository({"sub-a": games})
    client, validator, _publisher = _build(library_repository=library)
    validator.register("token-a", _claims(sub="sub-a"))

    hide = client.put(_path(client, library_routes.hide_game, game_id="g2"), headers=_bearer("token-a"))
    body = LibraryPageResponse.model_validate(
        client.get(_path(client, library_routes.get_library), headers=_bearer("token-a")).json()
    )

    assert hide.status_code == 204
    assert [row.game_id for row in body.games] == ["g1"]
    assert body.hidden_count == 1
    assert library.hidden_filters[-1] == HIDDEN_EXCLUDE


def test_hiding_a_game_the_caller_does_not_hold_is_404():
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": []}))
    validator.register("token-a", _claims(sub="sub-a"))

    assert (
        client.put(_path(client, library_routes.hide_game, game_id="g9"), headers=_bearer("token-a")).status_code == 404
    )


def test_the_hidden_view_lists_only_hidden_games_and_unhiding_is_idempotent():
    games = [FakeLibraryGameView("g1", "Above"), FakeLibraryGameView("g2", "Below")]
    library = FakeLibraryRepository({"sub-a": games}, hidden_game_ids=[("sub-a", "g2")])
    client, validator, _publisher = _build(library_repository=library)
    validator.register("token-a", _claims(sub="sub-a"))

    body = LibraryPageResponse.model_validate(
        client.get(_path(client, library_routes.get_library) + "?hidden=only", headers=_bearer("token-a")).json()
    )
    first = client.delete(_path(client, library_routes.unhide_game, game_id="g2"), headers=_bearer("token-a"))
    second = client.delete(_path(client, library_routes.unhide_game, game_id="g2"), headers=_bearer("token-a"))
    after = LibraryPageResponse.model_validate(
        client.get(_path(client, library_routes.get_library), headers=_bearer("token-a")).json()
    )

    assert [row.game_id for row in body.games] == ["g2"]
    assert (first.status_code, second.status_code) == (204, 204)
    assert [row.game_id for row in after.games] == ["g1", "g2"]
    assert after.hidden_count == 0


def test_get_library_requires_bearer_token():
    client, _validator, _publisher = _build()

    assert client.get(_path(client, library_routes.get_library)).status_code == 401


def test_get_library_returns_callers_own_games_with_ratings_and_genre():
    """The two rows pair the three provenance booleans against the ratings anti-correlated on purpose:
    game-1 carries a ``psn_rating`` with ``psn_enriched`` false (0049 deliberately backfilled nothing, so
    every row enriched before it reads false), and game-2 carries no rating at all with ``psn_enriched``
    true. A response field reconstructed from ``psn_rating`` is wrong on both."""
    games = [
        FakeLibraryGameView(
            "game-1",
            "Elden Ring",
            genre="Action RPG",
            rawg_rating=96.0,
            opencritic_rating=94.0,
            psn_rating=4.8,
            psn_product_id="UP0700-CUSA23100_00-ELDENRING0000000",
            rawg_enriched=True,
            opencritic_enriched=True,
            psn_enriched=False,
            cover_image_url="https://cdn.example/elden-ring.jpg",
            platforms=(PS5, PS4),
        ),
        FakeLibraryGameView(
            "game-2", "Store Enriched Only", rawg_enriched=False, opencritic_enriched=False, psn_enriched=True
        ),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library), headers=_bearer("token-a"))

    assert response.status_code == 200
    assert LibraryPageResponse.model_validate(response.json()) == LibraryPageResponse(
        games=[
            LibraryGameResponse(
                game_id="game-1",
                title="Elden Ring",
                genre="Action RPG",
                rawg_rating=96.0,
                opencritic_rating=94.0,
                psn_rating=4.8,
                psn_product_id="UP0700-CUSA23100_00-ELDENRING0000000",
                rawg_enriched=True,
                opencritic_enriched=True,
                psn_enriched=False,
                is_active=True,
                percent_completed=None,
                source=LIBRARY_SOURCE_PSN,
                cover_image_url="https://cdn.example/elden-ring.jpg",
                platforms=[PS5, PS4],
                trophy_match=TROPHY_NOT_ATTEMPTED,
            ),
            LibraryGameResponse(
                game_id="game-2",
                title="Store Enriched Only",
                genre=None,
                rawg_rating=None,
                opencritic_rating=None,
                psn_rating=None,
                psn_product_id=None,
                rawg_enriched=False,
                opencritic_enriched=False,
                psn_enriched=True,
                is_active=True,
                percent_completed=None,
                source=LIBRARY_SOURCE_PSN,
                cover_image_url=None,
                platforms=[],
                trophy_match=TROPHY_NOT_ATTEMPTED,
            ),
        ],
        total=2,
        trophy_progress=TrophyProgressResponse(state=TROPHY_PROGRESS_OFF, reason=TROPHY_PROGRESS_NO_LINK),
        hidden_count=0,
    )


def test_get_library_preserves_platform_order_from_the_repository():
    """Platforms arrive ordered newest-first by ``platforms.sort_order``; the route must not re-sort
    them into alphabetical order, which would read PS3 before PS5."""
    games = [FakeLibraryGameView("game-1", "99Vidas", platforms=(PS4, PS3, PSVITA))]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library), headers=_bearer("token-a"))

    assert response.status_code == 200
    assert LibraryPageResponse.model_validate(response.json()).games[0].platforms == [PS4, PS3, PSVITA]


def test_get_library_flags_a_game_the_caller_lost_access_to():

    games = [
        FakeLibraryGameView("game-1", "Still Mine"),
        FakeLibraryGameView("game-2", "Lapsed Plus Title", is_active=False),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library), headers=_bearer("token-a"))

    assert response.status_code == 200
    by_title = {game.title: game.is_active for game in LibraryPageResponse.model_validate(response.json()).games}
    assert by_title == {"Still Mine": True, "Lapsed Plus Title": False}


def test_get_library_returns_empty_page_for_a_user_with_no_entries():
    client, validator, _publisher = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library), headers=_bearer("token-a"))

    assert response.status_code == 200
    assert LibraryPageResponse.model_validate(response.json()) == LibraryPageResponse(
        games=[],
        total=0,
        trophy_progress=TrophyProgressResponse(state=TROPHY_PROGRESS_OFF, reason=TROPHY_PROGRESS_NO_LINK),
        hidden_count=0,
    )


def test_get_library_scoped_to_caller_only():
    games_a = [FakeLibraryGameView("game-1", "A's Game")]
    games_b = [FakeLibraryGameView("game-2", "B's Game")]
    client, validator, _publisher = _build(
        library_repository=FakeLibraryRepository({"sub-a": games_a, "sub-b": games_b})
    )
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library), headers=_bearer("token-a"))

    assert [game.title for game in LibraryPageResponse.model_validate(response.json()).games] == ["A's Game"]


def test_get_library_search_filters_by_title_substring_case_insensitively():
    games = [FakeLibraryGameView("game-1", "Elden Ring"), FakeLibraryGameView("game-2", "Bloodborne")]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library) + "?q=elden", headers=_bearer("token-a"))

    body = LibraryPageResponse.model_validate(response.json())
    assert [g.title for g in body.games] == ["Elden Ring"]
    assert body.total == 1


def test_get_library_genre_filters_exact_match():
    games = [
        FakeLibraryGameView("game-1", "Elden Ring", genre="Action RPG"),
        FakeLibraryGameView("game-2", "Tetris Effect", genre="Puzzle"),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library) + "?genre=Puzzle", headers=_bearer("token-a"))

    body = LibraryPageResponse.model_validate(response.json())
    assert [g.title for g in body.games] == ["Tetris Effect"]
    assert body.total == 1


def test_get_library_sort_by_rating_nulls_last_ascending_and_descending():
    games = [
        FakeLibraryGameView("g1", "No Rating"),
        FakeLibraryGameView("g2", "High", rawg_rating=90.0),
        FakeLibraryGameView("g3", "Low", rawg_rating=40.0),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    asc = LibraryPageResponse.model_validate(
        client.get(
            _path(client, library_routes.get_library) + "?sort=rawg_rating&sortDir=asc", headers=_bearer("token-a")
        ).json()
    )
    assert [g.title for g in asc.games] == ["Low", "High", "No Rating"]

    desc = LibraryPageResponse.model_validate(
        client.get(
            _path(client, library_routes.get_library) + "?sort=rawg_rating&sortDir=desc", headers=_bearer("token-a")
        ).json()
    )
    assert [g.title for g in desc.games] == ["High", "Low", "No Rating"]


def test_get_library_sort_by_percent_completed_nulls_last_ascending_and_descending():
    games = [
        FakeLibraryGameView("g1", "No Progress"),
        FakeLibraryGameView("g2", "Mostly Done", percent_completed=90),
        FakeLibraryGameView("g3", "Barely Started", percent_completed=10),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    asc = LibraryPageResponse.model_validate(
        client.get(
            _path(client, library_routes.get_library) + "?sort=percent_completed&sortDir=asc",
            headers=_bearer("token-a"),
        ).json()
    )
    assert [g.title for g in asc.games] == ["Barely Started", "Mostly Done", "No Progress"]

    desc = LibraryPageResponse.model_validate(
        client.get(
            _path(client, library_routes.get_library) + "?sort=percent_completed&sortDir=desc",
            headers=_bearer("token-a"),
        ).json()
    )
    assert [g.title for g in desc.games] == ["Mostly Done", "Barely Started", "No Progress"]


def test_get_library_rejects_unknown_sort_field():
    client, validator, _publisher = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(
        _path(client, library_routes.get_library) + "?sort=not_a_real_field", headers=_bearer("token-a")
    )

    assert response.status_code == 422


def test_get_library_pagination_limit_and_offset():
    games = [FakeLibraryGameView(f"g{i}", f"Game {i}") for i in range(5)]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library) + "?limit=2&offset=2", headers=_bearer("token-a"))

    body = LibraryPageResponse.model_validate(response.json())
    assert [g.title for g in body.games] == ["Game 2", "Game 3"]
    assert body.total == 5


def test_get_library_genres_returns_distinct_sorted_genres():
    games = [
        FakeLibraryGameView("g1", "A", genre="RPG"),
        FakeLibraryGameView("g2", "B", genre="Puzzle"),
        FakeLibraryGameView("g3", "C", genre="RPG"),
        FakeLibraryGameView("g4", "D", genre=None),
    ]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library_genres), headers=_bearer("token-a"))

    assert response.status_code == 200
    assert LibraryGenresResponse.model_validate(response.json()) == LibraryGenresResponse(genres=["Puzzle", "RPG"])


def test_get_library_genres_empty_for_user_with_no_enriched_genres():
    client, validator, _publisher = _build()
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library_genres), headers=_bearer("token-a"))

    assert response.status_code == 200
    assert LibraryGenresResponse.model_validate(response.json()) == LibraryGenresResponse(genres=[])


def test_get_library_percent_completed_comes_from_the_stored_column():
    games = [FakeLibraryGameView("game-1", "Game A", percent_completed=50)]
    repository = FakeRepository()
    _seed_link(repository, TokenCrypto(TokenCrypto.generate_key()), "sub-a", harvest_trophies=True)
    client, validator, _publisher = _build(
        library_repository=FakeLibraryRepository({"sub-a": games}, has_trophy_progress=True), repository=repository
    )
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library), headers=_bearer("token-a"))

    assert LibraryPageResponse.model_validate(response.json()).games[0].percent_completed == 50


def test_get_library_percent_completed_blank_for_unlinked_user():
    games = [FakeLibraryGameView("game-1", "God of War Ragnarök")]
    client, validator, _publisher = _build(library_repository=FakeLibraryRepository({"sub-a": games}))
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library), headers=_bearer("token-a"))

    assert LibraryPageResponse.model_validate(response.json()).games[0].percent_completed is None


def test_get_library_percent_completed_blank_when_harvest_trophies_disabled():
    games = [FakeLibraryGameView("game-1", "God of War Ragnarök")]
    repository = FakeRepository()
    crypto = TokenCrypto(TokenCrypto.generate_key())
    _seed_link(repository, crypto, "sub-a", harvest_trophies=False)
    client, validator, _publisher = _build(
        library_repository=FakeLibraryRepository({"sub-a": games}), repository=repository
    )
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library), headers=_bearer("token-a"))

    assert LibraryPageResponse.model_validate(response.json()).games[0].percent_completed is None


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
    crypto = TokenCrypto(TokenCrypto.generate_key())
    _seed_link(repository, crypto, "sub-a", harvest_trophies=True)
    factory = FakeTrophyClientFactory()
    factory.linked["sub-a"] = FakeTrophyClient()
    client, validator, _publisher = _build(
        library_repository=FakeLibraryRepository({"sub-a": games}, has_trophy_progress=True),
        repository=repository,
        trophy_client_factory=factory,
    )
    validator.register("token-a", _claims(sub="sub-a"))

    response = client.get(_path(client, library_routes.get_library), headers=_bearer("token-a"))

    by_title = {
        game.title: game.percent_completed for game in LibraryPageResponse.model_validate(response.json()).games
    }
    assert by_title == {"Game A": 63, "Game B": None}
    assert factory.calls == []
