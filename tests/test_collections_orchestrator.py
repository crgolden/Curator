"""Tests for CollectionOrchestrator, using a hand-written fake CollectionsRepository."""

from __future__ import annotations

import pytest

from curator.collections.collection_orchestrator import (
    _DEFAULT_SIZE_GB,
    BYTES_PER_GB,
    IGNORED_FILTER_MIN_PERCENT_COMPLETED,
    IGNORED_FILTER_REASON_NO_TROPHY_DATA,
    CollectionOrchestrator,
)
from curator.collections.collection_spec import CAPACITY_FILL_KIND, FILTER_LIST_KIND, CollectionSpec
from curator.collections.game_candidate import (
    CAPPED_DEFAULT_SIZE,
    DEFAULT_SIZE,
    DOWNLOAD_SIZE,
    ESTIMATED_SIZE,
    MEASURED_SIZE,
)
from curator.collections.repository import (
    STORAGE_KIND_M2,
    STORAGE_KIND_USB,
    RawCandidateRow,
    StorageDevice,
    UserConsole,
)
from curator.collections.sort_order import COMPOSITE_DESC
from curator.psn.title_platform import PS3, PS4, PS5, PSP
from curator.scoring.size_estimation_service import SizeEstimate
from test_values import (
    lowercase_token,
    new_capacity_gb,
    new_console_id,
    new_game_id,
    new_genre_name,
    new_identity_sub,
    new_install_size_gb,
    new_percent_above,
    new_percent_below,
    new_percent_completed,
    new_positive_count,
    new_review_score,
    new_review_score_below,
    new_size_gb_below,
    new_small_count,
    new_storage_device_id,
)

SUB = new_identity_sub()
_CONSOLE_ID = new_console_id()
_LARGE_CAPACITY_GB = new_capacity_gb()
_GENRE = new_genre_name()
_AAA_TIER = lowercase_token()
_CRITICAL_SCORE = new_review_score()
_AAA_ESTIMATE_GB = new_install_size_gb()

_SIZE_ESTIMATES = [
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=None,
        aaa_tier=_AAA_TIER,
        genre_class=None,
        platform=PS5,
        size_gb=_AAA_ESTIMATE_GB,
    ),
    SizeEstimate(
        estimate_id=lowercase_token(),
        title_pattern=None,
        aaa_tier="Indie",
        genre_class=None,
        platform=PS5,
        size_gb=new_install_size_gb(),
    ),
]


class FakeCollectionsRepository:
    def __init__(
        self,
        consoles=None,
        candidates=None,
        devices=None,
        *,
        has_trophy_data=False,
        missing_trophy_data=0,
        media_ceilings=None,
    ):
        self._consoles = consoles or []
        self._candidates = candidates or []
        self._devices = devices or []
        self._has_trophy_data = has_trophy_data
        self._missing_trophy_data = missing_trophy_data
        self._media_ceilings = dict(media_ceilings or {})
        self.list_candidates_calls: list[str | None] = []
        self.include_inactive_calls: list[bool] = []
        self.min_percent_completed_calls: list[int | None] = []
        self.exclude_installed_on_calls: list[tuple[str, ...] | None] = []
        self.include_non_games_calls: list[bool] = []
        self.missing_trophy_data_calls: list[str] = []
        self.list_user_consoles_call_count = 0

    async def list_user_consoles(self, identity_sub):
        self.list_user_consoles_call_count += 1
        return self._consoles

    async def list_storage_devices(self, identity_sub):
        return self._devices

    async def list_candidates(
        self,
        identity_sub,
        *,
        platform=None,
        include_inactive=False,
        min_percent_completed=None,
        exclude_installed_on=None,
        include_non_games=False,
    ):
        self.list_candidates_calls.append(platform)
        self.include_inactive_calls.append(include_inactive)
        self.min_percent_completed_calls.append(min_percent_completed)
        self.exclude_installed_on_calls.append(exclude_installed_on)
        self.include_non_games_calls.append(include_non_games)
        return self._candidates

    async def user_has_trophy_data(self, identity_sub):
        return self._has_trophy_data

    async def count_candidates_missing_trophy_data(
        self, identity_sub, *, platform=None, include_inactive=False, exclude_installed_on=None, include_non_games=False
    ):
        self.missing_trophy_data_calls.append(identity_sub)
        return self._missing_trophy_data

    async def list_platform_media_ceilings(self):
        return dict(self._media_ceilings)


def _row(
    game_id,
    *,
    genre=_GENRE,
    aaa_tier=_AAA_TIER,
    critical_score=_CRITICAL_SCORE,
    oc_score=None,
    psn_rating=None,
    is_free_to_play=False,
    measured_size_gb=None,
    title=None,
    download_size_bytes=None,
):
    return RawCandidateRow(
        game_id=game_id,
        title=title or game_id,
        genre=genre,
        aaa_tier=aaa_tier,
        franchise="",
        critical_score=critical_score,
        oc_score=oc_score,
        psn_rating=psn_rating,
        is_free_to_play=is_free_to_play,
        measured_size_gb=measured_size_gb,
        download_size_bytes=download_size_bytes,
    )


async def test_a_download_size_beats_an_estimate_and_a_measurement_beats_a_download():
    measured_gb = new_install_size_gb()
    download_bytes = new_small_count() * BYTES_PER_GB
    measured_id, downloaded_id = new_game_id(), new_game_id()
    repository = FakeCollectionsRepository(
        consoles=[_console(platform=PS5)],
        candidates=[
            _row(measured_id, measured_size_gb=measured_gb, download_size_bytes=download_bytes),
            _row(downloaded_id, download_size_bytes=download_bytes),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID), size_estimates=_SIZE_ESTIMATES
    )

    by_id = {c.game_id: c for c in (*result.included, *result.excluded)}
    assert (by_id[measured_id].size_gb, by_id[measured_id].size_source) == (measured_gb, MEASURED_SIZE)
    assert (by_id[downloaded_id].size_gb, by_id[downloaded_id].size_source) == (
        download_bytes / BYTES_PER_GB,
        DOWNLOAD_SIZE,
    )


async def test_a_psp_candidate_with_no_size_data_resolves_to_the_media_ceiling():
    media_ceiling_gb = new_size_gb_below(_DEFAULT_SIZE_GB)
    repository = FakeCollectionsRepository(
        consoles=[_console(platform=PSP)], candidates=[_row(new_game_id())], media_ceilings={PSP: media_ceiling_gb}
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID), size_estimates=_SIZE_ESTIMATES
    )

    candidate = (*result.included, *result.excluded)[0]
    assert (candidate.size_gb, candidate.size_source) == (media_ceiling_gb, CAPPED_DEFAULT_SIZE)


async def test_a_run_with_no_console_never_applies_a_media_ceiling():
    repository = FakeCollectionsRepository(candidates=[_row(new_game_id())], media_ceilings={PS4: 1.0})
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(SUB, CollectionSpec(kind=FILTER_LIST_KIND), size_estimates=[])

    assert (result.included[0].size_gb, result.included[0].size_source) == (_DEFAULT_SIZE_GB, DEFAULT_SIZE)


async def test_a_completion_floor_for_a_user_with_no_trophy_data_is_reported_as_ignored():
    repository = FakeCollectionsRepository(candidates=[_row(new_game_id())], has_trophy_data=False)
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=FILTER_LIST_KIND, min_percent_completed=new_percent_completed()), size_estimates=[]
    )

    assert [(item.filter, item.reason) for item in result.ignored_filters] == [
        (IGNORED_FILTER_MIN_PERCENT_COMPLETED, IGNORED_FILTER_REASON_NO_TROPHY_DATA)
    ]
    assert result.excluded_for_missing_trophy_data == 0
    assert repository.missing_trophy_data_calls == []


async def test_a_completion_floor_for_a_partially_harvested_user_reports_the_silently_dropped_count():
    dropped = new_positive_count()
    repository = FakeCollectionsRepository(
        candidates=[_row(new_game_id())], has_trophy_data=True, missing_trophy_data=dropped
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=FILTER_LIST_KIND, min_percent_completed=new_percent_completed()), size_estimates=[]
    )

    assert result.ignored_filters == ()
    assert result.excluded_for_missing_trophy_data == dropped


async def test_no_completion_floor_asks_nothing_about_trophy_data():
    repository = FakeCollectionsRepository(
        candidates=[_row(new_game_id())], has_trophy_data=True, missing_trophy_data=new_small_count()
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(SUB, CollectionSpec(kind=FILTER_LIST_KIND), size_estimates=[])

    assert result.excluded_for_missing_trophy_data == 0
    assert repository.missing_trophy_data_calls == []


async def test_a_capacity_fill_counts_every_content_kind_and_a_filter_list_does_not():
    repository = FakeCollectionsRepository(consoles=[_console(platform=PS5)], candidates=[_row(new_game_id())])
    orchestrator = CollectionOrchestrator(repository)

    await orchestrator.generate(SUB, CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID), size_estimates=[])
    await orchestrator.generate(SUB, CollectionSpec(kind=FILTER_LIST_KIND), size_estimates=[])

    assert repository.include_non_games_calls == [True, False]


async def test_capacity_fill_requires_console_id():
    orchestrator = CollectionOrchestrator(FakeCollectionsRepository())

    spec_without_console = CollectionSpec(kind=CAPACITY_FILL_KIND)

    with pytest.raises(ValueError, match="requires a console_id"):
        await orchestrator.generate(SUB, spec_without_console, size_estimates=[])


async def test_capacity_fill_requires_known_console():
    orchestrator = CollectionOrchestrator(FakeCollectionsRepository(consoles=[]))

    spec_with_missing_console = CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=new_console_id())

    with pytest.raises(ValueError, match="Unknown console_id"):
        await orchestrator.generate(SUB, spec_with_missing_console, size_estimates=[])


async def test_capacity_fill_uses_console_effective_capacity_and_platform():
    measured_size_gb = new_install_size_gb()
    console = UserConsole(
        console_id=_CONSOLE_ID,
        name=lowercase_token(),
        platform=PS5,
        raw_capacity_gb=new_capacity_gb(),
        update_buffer_gb=new_install_size_gb(),
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(
        consoles=[console], candidates=[_row(new_game_id(), measured_size_gb=measured_size_gb)]
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID), size_estimates=[]
    )

    assert repository.list_candidates_calls == [PS5]
    assert len(result.included) == 1
    assert result.used_gb == pytest.approx(measured_size_gb)


async def test_capacity_fill_uses_measured_size_over_estimate():
    measured_size_gb = new_install_size_gb()
    console = UserConsole(
        console_id=_CONSOLE_ID,
        name=PS5,
        platform=PS5,
        raw_capacity_gb=new_capacity_gb(),
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(
        consoles=[console], candidates=[_row(new_game_id(), measured_size_gb=measured_size_gb)]
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID), size_estimates=_SIZE_ESTIMATES
    )

    assert result.included[0].size_gb == measured_size_gb


async def test_capacity_fill_falls_back_to_estimate_when_no_measured_size():
    console = UserConsole(
        console_id=_CONSOLE_ID,
        name=PS5,
        platform=PS5,
        raw_capacity_gb=new_capacity_gb(),
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(
        consoles=[console], candidates=[_row(new_game_id(), aaa_tier=_AAA_TIER, measured_size_gb=None)]
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID), size_estimates=_SIZE_ESTIMATES
    )

    assert result.included[0].size_gb == _AAA_ESTIMATE_GB


async def _resolve_one(candidate_row, *, platform=PS5, size_estimates=_SIZE_ESTIMATES):
    console = UserConsole(
        console_id=_CONSOLE_ID,
        name=platform,
        platform=platform,
        raw_capacity_gb=new_capacity_gb(),
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(consoles=[console], candidates=[candidate_row])
    orchestrator = CollectionOrchestrator(repository)
    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID), size_estimates=size_estimates
    )
    return result.included[0]


async def test_a_contributed_measurement_reports_itself_as_measured():
    contributed_size_gb = new_install_size_gb()

    candidate = await _resolve_one(_row(new_game_id(), measured_size_gb=contributed_size_gb))

    assert (candidate.size_gb, candidate.size_source) == (contributed_size_gb, MEASURED_SIZE)


async def test_a_size_estimates_band_reports_itself_as_estimated():
    candidate = await _resolve_one(_row(new_game_id(), aaa_tier=_AAA_TIER, measured_size_gb=None))

    assert (candidate.size_gb, candidate.size_source) == (_AAA_ESTIMATE_GB, ESTIMATED_SIZE)


async def test_a_platform_with_no_seeded_band_reports_the_flat_fallback_as_default_not_as_an_estimate():
    """0033 seeds PS5 and PS4 bands only, so every PS3, Vita, PSP, PS2 and PS1 title packs at the flat
    size. Reporting that as an estimate makes a number nobody computed indistinguishable from one somebody
    did, and it is exactly the case a client should prompt its owner to measure."""
    candidate = await _resolve_one(_row(new_game_id(), aaa_tier=_AAA_TIER, measured_size_gb=None), platform=PS3)

    assert (candidate.size_gb, candidate.size_source) == (_DEFAULT_SIZE_GB, DEFAULT_SIZE)


def _device(
    device_id,
    *,
    identity_sub=SUB,
    console_id=_CONSOLE_ID,
    kind=STORAGE_KIND_M2,
    capacity_gb=_LARGE_CAPACITY_GB,
    buffer_gb=0.0,
):
    return StorageDevice(
        device_id=device_id,
        identity_sub=identity_sub,
        console_id=console_id,
        name=device_id,
        kind=kind,
        capacity_gb=capacity_gb,
        buffer_gb=buffer_gb,
    )


async def test_capacity_fill_spills_overflow_onto_an_attached_device():
    size_gb = new_install_size_gb()
    first_game_id, second_game_id = new_game_id(), new_game_id()
    console = UserConsole(
        console_id=_CONSOLE_ID,
        name=PS5,
        platform=PS5,
        raw_capacity_gb=size_gb,
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(
        consoles=[console],
        devices=[_device(new_storage_device_id(), kind=STORAGE_KIND_M2, capacity_gb=size_gb)],
        candidates=[
            _row(first_game_id, measured_size_gb=size_gb),
            _row(second_game_id, measured_size_gb=size_gb),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID), size_estimates=[]
    )

    assert {c.game_id for c in result.included} == {first_game_id, second_game_id}
    assert result.excluded == ()
    assert result.used_gb == pytest.approx(size_gb + size_gb)


async def test_capacity_fill_never_offers_usb_storage_to_a_ps5_console():
    game_size_gb = new_install_size_gb()
    game_id = new_game_id()
    console = UserConsole(
        console_id=_CONSOLE_ID,
        name=PS5,
        platform=PS5,
        raw_capacity_gb=new_size_gb_below(game_size_gb),
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(
        consoles=[console],
        devices=[_device(new_storage_device_id(), kind=STORAGE_KIND_USB, capacity_gb=new_capacity_gb())],
        candidates=[_row(game_id, measured_size_gb=game_size_gb)],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID), size_estimates=[]
    )

    assert result.included == ()
    assert [c.game_id for c in result.excluded] == [game_id]


async def test_capacity_fill_offers_usb_storage_to_a_ps4_console():
    game_size_gb = new_install_size_gb()
    game_id = new_game_id()
    console = UserConsole(
        console_id=_CONSOLE_ID,
        name=PS4,
        platform=PS4,
        raw_capacity_gb=new_size_gb_below(game_size_gb),
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(
        consoles=[console],
        devices=[_device(new_storage_device_id(), kind=STORAGE_KIND_USB, capacity_gb=new_capacity_gb())],
        candidates=[_row(game_id, measured_size_gb=game_size_gb)],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID), size_estimates=[]
    )

    assert [c.game_id for c in result.included] == [game_id]
    assert result.excluded == ()


async def test_capacity_fill_ignores_devices_attached_to_a_different_console():
    game_size_gb = new_install_size_gb()
    game_id = new_game_id()
    console = UserConsole(
        console_id=_CONSOLE_ID,
        name=PS5,
        platform=PS5,
        raw_capacity_gb=new_size_gb_below(game_size_gb),
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(
        consoles=[console],
        devices=[
            _device(
                new_storage_device_id(),
                console_id=new_console_id(),
                kind=STORAGE_KIND_M2,
                capacity_gb=new_capacity_gb(),
            )
        ],
        candidates=[_row(game_id, measured_size_gb=game_size_gb)],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID), size_estimates=[]
    )

    assert result.included == ()
    assert [c.game_id for c in result.excluded] == [game_id]


async def test_filter_list_does_not_require_console():
    repository = FakeCollectionsRepository(candidates=[_row(new_game_id())])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(SUB, CollectionSpec(kind=FILTER_LIST_KIND), size_estimates=[])

    assert repository.list_candidates_calls == [None]
    assert len(result.included) == 1
    assert result.used_gb is None


async def test_candidate_pool_excludes_inactive_entitlements_by_default():
    repository = FakeCollectionsRepository(candidates=[_row(new_game_id())])
    orchestrator = CollectionOrchestrator(repository)

    await orchestrator.generate(SUB, CollectionSpec(kind=FILTER_LIST_KIND), size_estimates=[])

    assert repository.include_inactive_calls == [False]


async def test_include_inactive_reaches_the_candidate_query():
    repository = FakeCollectionsRepository(candidates=[_row(new_game_id())])
    orchestrator = CollectionOrchestrator(repository)

    await orchestrator.generate(SUB, CollectionSpec(kind=FILTER_LIST_KIND, include_inactive=True), size_estimates=[])

    assert repository.include_inactive_calls == [True]


async def test_filter_list_excludes_non_matching_from_included_but_reports_excluded():
    matching_genre, other_genre = new_genre_name(), new_genre_name()
    matching_id, other_id = new_game_id(), new_game_id()
    repository = FakeCollectionsRepository(
        candidates=[_row(matching_id, genre=matching_genre), _row(other_id, genre=other_genre)]
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=FILTER_LIST_KIND, genre_filter=(matching_genre,)), size_estimates=[]
    )

    assert [c.game_id for c in result.included] == [matching_id]
    assert [c.game_id for c in result.excluded] == [other_id]


async def test_free_to_play_penalizes_rank_score():
    critical_score = new_review_score()
    free_to_play_id, paid_id = new_game_id(), new_game_id()
    repository = FakeCollectionsRepository(
        candidates=[
            _row(free_to_play_id, is_free_to_play=True, critical_score=critical_score),
            _row(paid_id, is_free_to_play=False, critical_score=critical_score),
        ]
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(SUB, CollectionSpec(kind=FILTER_LIST_KIND), size_estimates=[])

    by_id = {c.game_id: c for c in result.included}
    assert by_id[free_to_play_id].rank_score < by_id[paid_id].rank_score


async def test_missing_aaa_tier_stays_none_rather_than_indie_or_empty_string():
    """WP8: a game with no recorded tier must not silently satisfy an Indie-tier predicate/filter -- see
    CollectionOrchestrator._score's own comment for the full diagnosis (defaulting to "Indie" here
    previously misclassified two real-world titles).

    The absent value is ``None``. It was ``""`` until the null sweep reached this field: the WP8 fix
    needed *something* that matched no tier and ``""`` was the smallest change at the time, with the
    real correction deferred because it ripples into ``CollectionGameResponse`` and Librarian's item
    template. ``None`` is the honest spelling -- ``""`` is a value, and a value invites an equality
    check that should never succeed.
    """
    repository = FakeCollectionsRepository(candidates=[_row(new_game_id(), aaa_tier=None)])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(SUB, CollectionSpec(kind=FILTER_LIST_KIND), size_estimates=[])

    assert result.included[0].aaa_tier is None


async def test_missing_aaa_tier_does_not_satisfy_an_indie_tier_filter():
    untiered_id, indie_id = new_game_id(), new_game_id()
    repository = FakeCollectionsRepository(
        candidates=[_row(untiered_id, aaa_tier=None), _row(indie_id, aaa_tier="Indie")]
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=FILTER_LIST_KIND, aaa_tier_filter="Indie"), size_estimates=[]
    )

    assert [c.game_id for c in result.included] == [indie_id]
    assert [c.game_id for c in result.excluded] == [untiered_id]


async def test_composite_score_averages_available_sources():
    repository = FakeCollectionsRepository(
        candidates=[_row(new_game_id(), critical_score=80.0, oc_score=90.0, psn_rating=5.0)]
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(SUB, CollectionSpec(kind=FILTER_LIST_KIND), size_estimates=[])

    assert result.included[0].composite_score == pytest.approx((80 + 90 + 100) / 3, rel=1e-3)


async def test_completion_map_attaches_percent_completed_to_candidates():
    completed_id, untracked_id = new_game_id(), new_game_id()
    percent_completed = new_percent_completed()
    repository = FakeCollectionsRepository(candidates=[_row(completed_id), _row(untracked_id)])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB,
        CollectionSpec(kind=FILTER_LIST_KIND),
        size_estimates=[],
        completion_map={completed_id: percent_completed},
    )

    by_id = {c.game_id: c for c in result.included}
    assert by_id[completed_id].percent_completed == percent_completed
    assert by_id[untracked_id].percent_completed is None


async def test_min_percent_completed_applied_when_completion_available():
    floor = new_percent_completed()
    low_id, high_id = new_game_id(), new_game_id()
    repository = FakeCollectionsRepository(candidates=[_row(low_id), _row(high_id)])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB,
        CollectionSpec(kind=FILTER_LIST_KIND, min_percent_completed=floor),
        size_estimates=[],
        completion_map={low_id: new_percent_below(floor), high_id: new_percent_above(floor)},
        completion_available=True,
    )

    assert [c.game_id for c in result.included] == [high_id]


async def test_min_percent_completed_skipped_when_completion_unavailable():
    first_id, second_id = new_game_id(), new_game_id()
    repository = FakeCollectionsRepository(candidates=[_row(first_id), _row(second_id)])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB,
        CollectionSpec(kind=FILTER_LIST_KIND, min_percent_completed=new_percent_completed()),
        size_estimates=[],
        completion_map=None,
        completion_available=False,
    )

    assert {c.game_id for c in result.included} == {first_id, second_id}


def _console(
    console_id=_CONSOLE_ID, *, platform=PS5, raw_capacity_gb=_LARGE_CAPACITY_GB, update_buffer_gb=0.0, routing_genres=()
):
    return UserConsole(
        console_id=console_id,
        name=console_id,
        platform=platform,
        raw_capacity_gb=raw_capacity_gb,
        update_buffer_gb=update_buffer_gb,
        routing_genres=routing_genres,
        fill_order=0,
    )


async def test_capacity_fill_now_applies_genre_filter_before_packing():
    """Previously capacity_fill ignored genre_filter/min_score/aaa_tier_filter/filter_predicate entirely
    -- only routing_genres (a console property) ever filtered this pool. Reproducing the legacy PS4
    Criterion/Blockbuster rule (Tools/PlayStation/LIFECYCLE_AUDIT.md) needs both a genre predicate and GB
    packing in the same run."""
    matching_genre, other_genre = new_genre_name(), new_genre_name()
    matching_id = new_game_id()
    repository = FakeCollectionsRepository(
        consoles=[_console()],
        candidates=[
            _row(matching_id, genre=matching_genre, measured_size_gb=new_install_size_gb()),
            _row(new_game_id(), genre=other_genre, measured_size_gb=new_install_size_gb()),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB,
        CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID, genre_filter=(matching_genre,)),
        size_estimates=[],
    )

    assert [c.game_id for c in result.included] == [matching_id]


async def test_capacity_fill_predicate_filtering_is_safe_for_every_existing_spec():
    """No existing capacity_fill spec has ever set genre_filter/min_score/aaa_tier_filter/filter_predicate
    -- confirms the new pre-filter step is a genuine no-op (pool unchanged) when none of those are set,
    so this change can't silently narrow an already-saved definition's results."""
    first_id, second_id = new_game_id(), new_game_id()
    repository = FakeCollectionsRepository(
        consoles=[_console()],
        candidates=[
            _row(first_id, measured_size_gb=new_install_size_gb()),
            _row(second_id, measured_size_gb=new_install_size_gb()),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID), size_estimates=[]
    )

    assert {c.game_id for c in result.included} == {first_id, second_id}


async def test_capacity_fill_unmatched_is_distinct_from_capacity_overflow():
    """unmatched (didn't satisfy the spec's own filter at all) and excluded (matched but didn't fit) are
    two different buckets -- the distinction collection chaining needs (see CollectionResult's own
    docstring)."""
    size_gb = new_install_size_gb()
    matching_genre, other_genre = new_genre_name(), new_genre_name()
    fits_id, overflows_id, wrong_genre_id = new_game_id(), new_game_id(), new_game_id()
    repository = FakeCollectionsRepository(
        consoles=[_console(raw_capacity_gb=size_gb)],
        candidates=[
            _row(fits_id, genre=matching_genre, measured_size_gb=size_gb),
            _row(overflows_id, genre=matching_genre, measured_size_gb=size_gb),
            _row(wrong_genre_id, genre=other_genre, measured_size_gb=size_gb),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB,
        CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID, genre_filter=(matching_genre,)),
        size_estimates=[],
    )

    assert [c.game_id for c in result.included] == [fits_id]
    assert [c.game_id for c in result.excluded] == [overflows_id]
    assert [c.game_id for c in result.unmatched] == [wrong_genre_id]


async def test_filter_list_unmatched_is_always_empty():
    """filter_list has no capacity constraint, so excluded already means exactly what unmatched would --
    unmatched stays empty rather than double-reporting the same games two different ways."""
    matching_genre, other_genre = new_genre_name(), new_genre_name()
    repository = FakeCollectionsRepository(
        candidates=[_row(new_game_id(), genre=matching_genre), _row(new_game_id(), genre=other_genre)]
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB, CollectionSpec(kind=FILTER_LIST_KIND, genre_filter=(matching_genre,)), size_estimates=[]
    )

    assert result.unmatched == ()


async def test_chained_candidate_ids_bypass_the_specs_own_predicate():
    """The legacy PS4 cascade's whole point: Blockbuster's input is blockbuster_candidates (normally
    filtered) *plus* criterion_overflow/uncategorised, unfiltered -- a chained-in game id must not be
    re-excluded just because it doesn't match this run's own genre filter."""
    filtered_genre = new_genre_name()
    blockbuster_match_id, chained_in_id, neither_id = new_game_id(), new_game_id(), new_game_id()
    repository = FakeCollectionsRepository(
        consoles=[_console()],
        candidates=[
            _row(blockbuster_match_id, genre=filtered_genre, measured_size_gb=new_install_size_gb()),
            _row(chained_in_id, genre=new_genre_name(), measured_size_gb=new_install_size_gb()),
            _row(neither_id, genre=new_genre_name(), measured_size_gb=new_install_size_gb()),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB,
        CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID, genre_filter=(filtered_genre,)),
        size_estimates=[],
        chained_candidate_ids=(chained_in_id,),
    )

    assert {c.game_id for c in result.included} == {blockbuster_match_id, chained_in_id}
    assert [c.game_id for c in result.unmatched] == [neither_id]


async def test_chained_candidate_ids_order_breaks_ties():
    """With sort_order=COMPOSITE_DESC (no rank_score tiebreak), two exactly-tied candidates fall back to
    input order -- chained_candidate_ids' own sequence order, not candidate id or pool order, decides
    which one wins when only one fits. This is what let the legacy PS4 cascade's exact
    ``criterion_overflow`` (already composite-sorted) before ``uncategorised`` concatenation order
    reproduce its real tie-breaks; getting this wrong is what caused an early version of this reproduction
    to land one title off (Tools/PlayStation/LIFECYCLE_AUDIT.md)."""
    size_gb = new_install_size_gb()
    tied_score = new_review_score()
    row_genre, filtered_genre = new_genre_name(), new_genre_name()
    only_room_for_one_a, only_room_for_one_b = new_game_id(), new_game_id()
    repository = FakeCollectionsRepository(
        consoles=[_console(raw_capacity_gb=size_gb)],
        candidates=[
            _row(only_room_for_one_a, genre=row_genre, measured_size_gb=size_gb, critical_score=tied_score),
            _row(only_room_for_one_b, genre=row_genre, measured_size_gb=size_gb, critical_score=tied_score),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    spec = CollectionSpec(
        kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID, genre_filter=(filtered_genre,), sort_order=COMPOSITE_DESC
    )
    result_a_first = await orchestrator.generate(
        SUB, spec, size_estimates=[], chained_candidate_ids=(only_room_for_one_a, only_room_for_one_b)
    )
    result_b_first = await orchestrator.generate(
        SUB, spec, size_estimates=[], chained_candidate_ids=(only_room_for_one_b, only_room_for_one_a)
    )

    assert [c.game_id for c in result_a_first.included] == [only_room_for_one_a]
    assert [c.game_id for c in result_b_first.included] == [only_room_for_one_b]


async def test_chained_candidate_ids_not_double_counted_when_already_matched():
    matching_genre = new_genre_name()
    game_id = new_game_id()
    repository = FakeCollectionsRepository(
        consoles=[_console()], candidates=[_row(game_id, genre=matching_genre, measured_size_gb=new_install_size_gb())]
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB,
        CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID, genre_filter=(matching_genre,)),
        size_estimates=[],
        chained_candidate_ids=(game_id,),
    )

    assert [c.game_id for c in result.included] == [game_id]


async def test_sort_order_reaches_capacity_fill_packing():
    higher_score = new_review_score()
    high_rank_low_composite, low_rank_high_composite = new_game_id(), new_game_id()
    repository = FakeCollectionsRepository(
        consoles=[_console()],
        candidates=[
            _row(high_rank_low_composite, measured_size_gb=new_install_size_gb(), critical_score=higher_score),
            _row(
                low_rank_high_composite,
                measured_size_gb=new_install_size_gb(),
                critical_score=new_review_score_below(higher_score),
            ),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        SUB,
        CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID, sort_order=COMPOSITE_DESC),
        size_estimates=[],
    )

    assert next(c.game_id for c in result.included) == high_rank_low_composite


async def test_exclude_installed_on_reaches_the_candidate_query():
    excluded_console_id = new_console_id()
    repository = FakeCollectionsRepository(
        consoles=[_console(console_id=_CONSOLE_ID), _console(console_id=excluded_console_id)], candidates=[]
    )
    orchestrator = CollectionOrchestrator(repository)

    await orchestrator.generate(
        SUB, CollectionSpec(kind=FILTER_LIST_KIND, exclude_installed_on=(excluded_console_id,)), size_estimates=[]
    )

    assert repository.exclude_installed_on_calls == [(excluded_console_id,)]


async def test_exclude_installed_on_rejects_a_console_that_is_not_the_callers_own():
    repository = FakeCollectionsRepository(consoles=[_console(console_id=_CONSOLE_ID)])
    orchestrator = CollectionOrchestrator(repository)

    spec = CollectionSpec(kind=FILTER_LIST_KIND, exclude_installed_on=(new_console_id(),))
    with pytest.raises(ValueError, match="Unknown console_id"):
        await orchestrator.generate(SUB, spec, size_estimates=[])


async def test_exclude_installed_on_fetches_consoles_only_once_for_capacity_fill():
    """capacity_fill already needs list_user_consoles for its own console/bin resolution -- exclude_
    installed_on's ownership check must reuse that same fetch, not issue a second query."""
    repository = FakeCollectionsRepository(
        consoles=[_console(console_id=_CONSOLE_ID)],
        candidates=[_row(new_game_id(), measured_size_gb=new_install_size_gb())],
    )
    orchestrator = CollectionOrchestrator(repository)

    await orchestrator.generate(
        SUB,
        CollectionSpec(kind=CAPACITY_FILL_KIND, console_id=_CONSOLE_ID, exclude_installed_on=(_CONSOLE_ID,)),
        size_estimates=[],
    )

    assert repository.list_user_consoles_call_count == 1


async def test_filter_list_with_no_exclude_installed_on_never_fetches_consoles():
    """The common case -- filter_list, no exclude_installed_on -- must cost zero extra queries versus
    before this feature existed."""
    repository = FakeCollectionsRepository(candidates=[_row(new_game_id())])
    orchestrator = CollectionOrchestrator(repository)

    await orchestrator.generate(SUB, CollectionSpec(kind=FILTER_LIST_KIND), size_estimates=[])

    assert repository.list_user_consoles_call_count == 0
