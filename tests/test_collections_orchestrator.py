"""Tests for CollectionOrchestrator, using a hand-written fake CollectionsRepository."""

from __future__ import annotations

import pytest

from curator.collections.collection_orchestrator import CollectionOrchestrator
from curator.collections.collection_spec import CollectionSpec
from curator.collections.repository import RawCandidateRow, StorageDevice, UserConsole
from curator.scoring.size_estimation_service import SizeEstimate

_SIZE_ESTIMATES = [
    SizeEstimate(estimate_id="1", title_pattern=None, aaa_tier="AAA", genre_class=None, platform="PS5", size_gb=59),
    SizeEstimate(estimate_id="2", title_pattern=None, aaa_tier="Indie", genre_class=None, platform="PS5", size_gb=16),
]


class FakeCollectionsRepository:
    def __init__(self, consoles=None, candidates=None, devices=None):
        self._consoles = consoles or []
        self._candidates = candidates or []
        self._devices = devices or []
        self.list_candidates_calls: list[str | None] = []
        self.include_inactive_calls: list[bool] = []
        self.min_percent_completed_calls: list[int | None] = []

    async def list_user_consoles(self, identity_sub):
        return self._consoles

    async def list_storage_devices(self, identity_sub):
        return self._devices

    async def list_candidates(self, identity_sub, *, platform=None, include_inactive=False, min_percent_completed=None):
        self.list_candidates_calls.append(platform)
        self.include_inactive_calls.append(include_inactive)
        self.min_percent_completed_calls.append(min_percent_completed)
        return self._candidates


def _row(
    game_id,
    *,
    genre="RPG",
    aaa_tier="AAA",
    critical_score=90.0,
    oc_score=None,
    psn_rating=None,
    is_free_to_play=False,
    measured_size_gb=None,
    title=None,
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
    )


async def test_capacity_fill_requires_console_id():
    orchestrator = CollectionOrchestrator(FakeCollectionsRepository())

    with pytest.raises(ValueError, match="requires a console_id"):
        await orchestrator.generate("sub-1", CollectionSpec(kind="capacity_fill"), size_estimates=[])


async def test_capacity_fill_requires_known_console():
    orchestrator = CollectionOrchestrator(FakeCollectionsRepository(consoles=[]))

    with pytest.raises(ValueError, match="Unknown console_id"):
        await orchestrator.generate(
            "sub-1", CollectionSpec(kind="capacity_fill", console_id="missing"), size_estimates=[]
        )


async def test_capacity_fill_uses_console_effective_capacity_and_platform():
    console = UserConsole(
        console_id="c1",
        name="My PS5",
        platform="PS5",
        raw_capacity_gb=100.0,
        update_buffer_gb=20.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(consoles=[console], candidates=[_row("g1", measured_size_gb=50.0)])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="capacity_fill", console_id="c1"), size_estimates=[]
    )

    assert repository.list_candidates_calls == ["PS5"]
    assert len(result.included) == 1
    assert result.used_gb == 50.0


async def test_capacity_fill_uses_measured_size_over_estimate():
    console = UserConsole(
        console_id="c1",
        name="PS5",
        platform="PS5",
        raw_capacity_gb=1000.0,
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(consoles=[console], candidates=[_row("g1", measured_size_gb=77.0)])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="capacity_fill", console_id="c1"), size_estimates=_SIZE_ESTIMATES
    )

    assert result.included[0].size_gb == 77.0


async def test_capacity_fill_falls_back_to_estimate_when_no_measured_size():
    console = UserConsole(
        console_id="c1",
        name="PS5",
        platform="PS5",
        raw_capacity_gb=1000.0,
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(
        consoles=[console], candidates=[_row("g1", aaa_tier="AAA", measured_size_gb=None)]
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="capacity_fill", console_id="c1"), size_estimates=_SIZE_ESTIMATES
    )

    assert result.included[0].size_gb == 59.0


def _device(device_id, *, identity_sub="sub-1", console_id="c1", kind="m2", capacity_gb=500.0, buffer_gb=0.0):
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
    console = UserConsole(
        console_id="c1",
        name="PS5",
        platform="PS5",
        raw_capacity_gb=60.0,
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(
        consoles=[console],
        devices=[_device("d1", kind="m2", capacity_gb=60.0)],
        candidates=[
            _row("a", measured_size_gb=60.0),
            _row("b", measured_size_gb=60.0),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="capacity_fill", console_id="c1"), size_estimates=[]
    )

    # Both titles fit -- one on the console's own drive, one on the attached M.2 -- because the two
    # capacities are separate bins rather than pooled into one 120GB number a single-bin fill would use.
    assert {c.game_id for c in result.included} == {"a", "b"}
    assert result.excluded == ()
    assert result.used_gb == 120.0


async def test_capacity_fill_never_offers_usb_storage_to_a_ps5_console():
    console = UserConsole(
        console_id="c1",
        name="PS5",
        platform="PS5",
        raw_capacity_gb=10.0,
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(
        consoles=[console],
        devices=[_device("d1", kind="usb", capacity_gb=1000.0)],
        candidates=[_row("a", measured_size_gb=60.0)],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="capacity_fill", console_id="c1"), size_estimates=[]
    )

    # A PS5 title cannot run from USB storage -- the attached USB device's ample capacity must never
    # absorb it, so it overflows despite the device having room to spare.
    assert result.included == ()
    assert [c.game_id for c in result.excluded] == ["a"]


async def test_capacity_fill_offers_usb_storage_to_a_ps4_console():
    console = UserConsole(
        console_id="c1",
        name="PS4",
        platform="PS4",
        raw_capacity_gb=10.0,
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(
        consoles=[console],
        devices=[_device("d1", kind="usb", capacity_gb=1000.0)],
        candidates=[_row("a", measured_size_gb=60.0)],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="capacity_fill", console_id="c1"), size_estimates=[]
    )

    assert [c.game_id for c in result.included] == ["a"]
    assert result.excluded == ()


async def test_capacity_fill_ignores_devices_attached_to_a_different_console():
    console = UserConsole(
        console_id="c1",
        name="PS5",
        platform="PS5",
        raw_capacity_gb=10.0,
        update_buffer_gb=0.0,
        routing_genres=(),
        fill_order=0,
    )
    repository = FakeCollectionsRepository(
        consoles=[console],
        devices=[_device("d1", console_id="some-other-console", kind="m2", capacity_gb=1000.0)],
        candidates=[_row("a", measured_size_gb=60.0)],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="capacity_fill", console_id="c1"), size_estimates=[]
    )

    assert result.included == ()
    assert [c.game_id for c in result.excluded] == ["a"]


async def test_filter_list_does_not_require_console():
    repository = FakeCollectionsRepository(candidates=[_row("g1")])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate("sub-1", CollectionSpec(kind="filter_list"), size_estimates=[])

    assert repository.list_candidates_calls == [None]
    assert len(result.included) == 1
    assert result.used_gb is None


async def test_candidate_pool_excludes_inactive_entitlements_by_default():
    # A collection is normally built from what its owner can actually launch. The filter lives in
    # list_candidates -- the single chokepoint -- so every strategy inherits it.
    repository = FakeCollectionsRepository(candidates=[_row("g1")])
    orchestrator = CollectionOrchestrator(repository)

    await orchestrator.generate("sub-1", CollectionSpec(kind="filter_list"), size_estimates=[])

    assert repository.include_inactive_calls == [False]


async def test_include_inactive_reaches_the_candidate_query():
    repository = FakeCollectionsRepository(candidates=[_row("g1")])
    orchestrator = CollectionOrchestrator(repository)

    await orchestrator.generate("sub-1", CollectionSpec(kind="filter_list", include_inactive=True), size_estimates=[])

    assert repository.include_inactive_calls == [True]


async def test_filter_list_excludes_non_matching_from_included_but_reports_excluded():
    repository = FakeCollectionsRepository(candidates=[_row("g1", genre="RPG"), _row("g2", genre="Sports")])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="filter_list", genre_filter=("RPG",)), size_estimates=[]
    )

    assert [c.game_id for c in result.included] == ["g1"]
    assert [c.game_id for c in result.excluded] == ["g2"]


async def test_free_to_play_penalizes_rank_score():
    repository = FakeCollectionsRepository(
        candidates=[
            _row("f2p", is_free_to_play=True, critical_score=90.0),
            _row("paid", is_free_to_play=False, critical_score=90.0),
        ]
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate("sub-1", CollectionSpec(kind="filter_list"), size_estimates=[])

    by_id = {c.game_id: c for c in result.included}
    assert by_id["f2p"].rank_score < by_id["paid"].rank_score


async def test_composite_score_averages_available_sources():
    repository = FakeCollectionsRepository(candidates=[_row("g1", critical_score=80.0, oc_score=90.0, psn_rating=5.0)])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate("sub-1", CollectionSpec(kind="filter_list"), size_estimates=[])

    assert result.included[0].composite_score == pytest.approx((80 + 90 + 100) / 3, rel=1e-3)


async def test_completion_map_attaches_percent_completed_to_candidates():
    repository = FakeCollectionsRepository(candidates=[_row("g1"), _row("g2")])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="filter_list"), size_estimates=[], completion_map={"g1": 75}
    )

    by_id = {c.game_id: c for c in result.included}
    assert by_id["g1"].percent_completed == 75
    assert by_id["g2"].percent_completed is None


async def test_min_percent_completed_applied_when_completion_available():
    repository = FakeCollectionsRepository(candidates=[_row("low"), _row("high")])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1",
        CollectionSpec(kind="filter_list", min_percent_completed=50),
        size_estimates=[],
        completion_map={"low": 10, "high": 90},
        completion_available=True,
    )

    assert [c.game_id for c in result.included] == ["high"]


async def test_min_percent_completed_skipped_when_completion_unavailable():
    repository = FakeCollectionsRepository(candidates=[_row("g1"), _row("g2")])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1",
        CollectionSpec(kind="filter_list", min_percent_completed=50),
        size_estimates=[],
        completion_map=None,
        completion_available=False,
    )

    assert {c.game_id for c in result.included} == {"g1", "g2"}
