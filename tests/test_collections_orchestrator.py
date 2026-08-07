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
        self.exclude_installed_on_calls: list[tuple[str, ...] | None] = []
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
    ):
        self.list_candidates_calls.append(platform)
        self.include_inactive_calls.append(include_inactive)
        self.min_percent_completed_calls.append(min_percent_completed)
        self.exclude_installed_on_calls.append(exclude_installed_on)
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


async def test_missing_aaa_tier_defaults_to_empty_string_not_indie():
    """WP8: a game with no recorded tier must not silently satisfy an Indie-tier predicate/filter -- see
    CollectionOrchestrator._score's own comment and AGENTS/PARKING_LOT.md's WP8 section for the full
    diagnosis (defaulting to "Indie" here previously misclassified two real-world titles)."""
    repository = FakeCollectionsRepository(candidates=[_row("g1", aaa_tier=None)])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate("sub-1", CollectionSpec(kind="filter_list"), size_estimates=[])

    assert result.included[0].aaa_tier == ""


async def test_missing_aaa_tier_does_not_satisfy_an_indie_tier_filter():
    repository = FakeCollectionsRepository(candidates=[_row("g1", aaa_tier=None), _row("g2", aaa_tier="Indie")])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="filter_list", aaa_tier_filter="Indie"), size_estimates=[]
    )

    assert [c.game_id for c in result.included] == ["g2"]
    assert [c.game_id for c in result.excluded] == ["g1"]


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


def _console(console_id="c1", *, platform="PS5", raw_capacity_gb=1000.0, update_buffer_gb=0.0, routing_genres=()):
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
    repository = FakeCollectionsRepository(
        consoles=[_console()],
        candidates=[
            _row("rpg", genre="RPG", measured_size_gb=10.0),
            _row("sports", genre="Sports", measured_size_gb=10.0),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="capacity_fill", console_id="c1", genre_filter=("RPG",)), size_estimates=[]
    )

    assert [c.game_id for c in result.included] == ["rpg"]


async def test_capacity_fill_predicate_filtering_is_safe_for_every_existing_spec():
    """No existing capacity_fill spec has ever set genre_filter/min_score/aaa_tier_filter/filter_predicate
    -- confirms the new pre-filter step is a genuine no-op (pool unchanged) when none of those are set,
    so this change can't silently narrow an already-saved definition's results."""
    repository = FakeCollectionsRepository(
        consoles=[_console()], candidates=[_row("a", measured_size_gb=10.0), _row("b", measured_size_gb=10.0)]
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="capacity_fill", console_id="c1"), size_estimates=[]
    )

    assert {c.game_id for c in result.included} == {"a", "b"}


async def test_capacity_fill_unmatched_is_distinct_from_capacity_overflow():
    """unmatched (didn't satisfy the spec's own filter at all) and excluded (matched but didn't fit) are
    two different buckets -- the distinction collection chaining needs (see CollectionResult's own
    docstring)."""
    repository = FakeCollectionsRepository(
        consoles=[_console(raw_capacity_gb=10.0)],
        candidates=[
            _row("fits", genre="RPG", measured_size_gb=10.0),
            _row("overflows", genre="RPG", measured_size_gb=10.0),
            _row("wrong_genre", genre="Sports", measured_size_gb=10.0),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="capacity_fill", console_id="c1", genre_filter=("RPG",)), size_estimates=[]
    )

    assert [c.game_id for c in result.included] == ["fits"]
    assert [c.game_id for c in result.excluded] == ["overflows"]
    assert [c.game_id for c in result.unmatched] == ["wrong_genre"]


async def test_filter_list_unmatched_is_always_empty():
    """filter_list has no capacity constraint, so excluded already means exactly what unmatched would --
    unmatched stays empty rather than double-reporting the same games two different ways."""
    repository = FakeCollectionsRepository(candidates=[_row("g1", genre="RPG"), _row("g2", genre="Sports")])
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="filter_list", genre_filter=("RPG",)), size_estimates=[]
    )

    assert result.unmatched == ()


async def test_chained_candidate_ids_bypass_the_specs_own_predicate():
    """The legacy PS4 cascade's whole point: Blockbuster's input is blockbuster_candidates (normally
    filtered) *plus* criterion_overflow/uncategorised, unfiltered -- a chained-in game id must not be
    re-excluded just because it doesn't match this run's own genre filter."""
    repository = FakeCollectionsRepository(
        consoles=[_console()],
        candidates=[
            _row("blockbuster_match", genre="Shooter", measured_size_gb=10.0),
            _row("chained_in", genre="RPG", measured_size_gb=10.0),
            _row("neither", genre="Puzzle", measured_size_gb=10.0),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1",
        CollectionSpec(kind="capacity_fill", console_id="c1", genre_filter=("Shooter",)),
        size_estimates=[],
        chained_candidate_ids=("chained_in",),
    )

    assert {c.game_id for c in result.included} == {"blockbuster_match", "chained_in"}
    assert [c.game_id for c in result.unmatched] == ["neither"]


async def test_chained_candidate_ids_order_breaks_ties():
    """With sort_order="composite_desc" (no rank_score tiebreak), two exactly-tied candidates fall back to
    input order -- chained_candidate_ids' own sequence order, not candidate id or pool order, decides
    which one wins when only one fits. This is what let the legacy PS4 cascade's exact
    ``criterion_overflow`` (already composite-sorted) before ``uncategorised`` concatenation order
    reproduce its real tie-breaks; getting this wrong is what caused an early version of this reproduction
    to land one title off (Tools/PlayStation/LIFECYCLE_AUDIT.md)."""
    repository = FakeCollectionsRepository(
        consoles=[_console(raw_capacity_gb=10.0)],
        candidates=[
            _row("only_room_for_one_a", genre="RPG", measured_size_gb=10.0, critical_score=50.0),
            _row("only_room_for_one_b", genre="RPG", measured_size_gb=10.0, critical_score=50.0),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    spec = CollectionSpec(kind="capacity_fill", console_id="c1", genre_filter=("Sports",), sort_order="composite_desc")
    result_a_first = await orchestrator.generate(
        "sub-1", spec, size_estimates=[], chained_candidate_ids=("only_room_for_one_a", "only_room_for_one_b")
    )
    result_b_first = await orchestrator.generate(
        "sub-1", spec, size_estimates=[], chained_candidate_ids=("only_room_for_one_b", "only_room_for_one_a")
    )

    assert [c.game_id for c in result_a_first.included] == ["only_room_for_one_a"]
    assert [c.game_id for c in result_b_first.included] == ["only_room_for_one_b"]


async def test_chained_candidate_ids_not_double_counted_when_already_matched():
    repository = FakeCollectionsRepository(
        consoles=[_console()], candidates=[_row("g1", genre="RPG", measured_size_gb=10.0)]
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1",
        CollectionSpec(kind="capacity_fill", console_id="c1", genre_filter=("RPG",)),
        size_estimates=[],
        chained_candidate_ids=("g1",),
    )

    assert [c.game_id for c in result.included] == ["g1"]


async def test_sort_order_reaches_capacity_fill_packing():
    repository = FakeCollectionsRepository(
        consoles=[_console(raw_capacity_gb=100.0)],
        candidates=[
            _row("high_rank_low_composite", measured_size_gb=10.0),
            _row("low_rank_high_composite", measured_size_gb=10.0, critical_score=10.0),
        ],
    )
    orchestrator = CollectionOrchestrator(repository)

    result = await orchestrator.generate(
        "sub-1", CollectionSpec(kind="capacity_fill", console_id="c1", sort_order="composite_desc"), size_estimates=[]
    )

    assert next(c.game_id for c in result.included) == "high_rank_low_composite"


async def test_exclude_installed_on_reaches_the_candidate_query():
    repository = FakeCollectionsRepository(
        consoles=[_console(console_id="c1"), _console(console_id="c2")], candidates=[]
    )
    orchestrator = CollectionOrchestrator(repository)

    await orchestrator.generate(
        "sub-1", CollectionSpec(kind="filter_list", exclude_installed_on=("c2",)), size_estimates=[]
    )

    assert repository.exclude_installed_on_calls == [("c2",)]


async def test_exclude_installed_on_rejects_a_console_that_is_not_the_callers_own():
    repository = FakeCollectionsRepository(consoles=[_console(console_id="c1")])
    orchestrator = CollectionOrchestrator(repository)

    spec = CollectionSpec(kind="filter_list", exclude_installed_on=("someone-elses-console",))
    with pytest.raises(ValueError, match="Unknown console_id"):
        await orchestrator.generate("sub-1", spec, size_estimates=[])


async def test_exclude_installed_on_fetches_consoles_only_once_for_capacity_fill():
    """capacity_fill already needs list_user_consoles for its own console/bin resolution -- exclude_
    installed_on's ownership check must reuse that same fetch, not issue a second query."""
    repository = FakeCollectionsRepository(
        consoles=[_console(console_id="c1")], candidates=[_row("g1", measured_size_gb=10.0)]
    )
    orchestrator = CollectionOrchestrator(repository)

    await orchestrator.generate(
        "sub-1",
        CollectionSpec(kind="capacity_fill", console_id="c1", exclude_installed_on=("c1",)),
        size_estimates=[],
    )

    assert repository.list_user_consoles_call_count == 1


async def test_filter_list_with_no_exclude_installed_on_never_fetches_consoles():
    """The common case -- filter_list, no exclude_installed_on -- must cost zero extra queries versus
    before this feature existed."""
    repository = FakeCollectionsRepository(candidates=[_row("g1")])
    orchestrator = CollectionOrchestrator(repository)

    await orchestrator.generate("sub-1", CollectionSpec(kind="filter_list"), size_estimates=[])

    assert repository.list_user_consoles_call_count == 0
