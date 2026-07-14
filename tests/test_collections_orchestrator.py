"""Tests for CollectionOrchestrator, using a hand-written fake CollectionsRepository."""

from __future__ import annotations

import pytest

from curator.collections.collection_orchestrator import CollectionOrchestrator
from curator.collections.collection_spec import CollectionSpec
from curator.collections.repository import RawCandidateRow, UserConsole
from curator.scoring.size_estimation_service import SizeEstimate

_SIZE_ESTIMATES = [
    SizeEstimate(estimate_id="1", title_pattern=None, aaa_tier="AAA", genre_class=None, platform="PS5", size_gb=59),
    SizeEstimate(estimate_id="2", title_pattern=None, aaa_tier="Indie", genre_class=None, platform="PS5", size_gb=16),
]


class FakeCollectionsRepository:
    def __init__(self, consoles=None, candidates=None):
        self._consoles = consoles or []
        self._candidates = candidates or []
        self.list_candidates_calls: list[str | None] = []

    async def list_user_consoles(self, identity_sub):
        return self._consoles

    async def list_candidates(self, identity_sub, *, platform=None):
        self.list_candidates_calls.append(platform)
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


async def test_filter_list_does_not_require_console():
    repository = FakeCollectionsRepository(candidates=[_row("g1")])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate("sub-1", CollectionSpec(kind="filter_list"), size_estimates=[])

    assert repository.list_candidates_calls == [None]
    assert len(result.included) == 1
    assert result.used_gb is None


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
