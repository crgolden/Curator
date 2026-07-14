"""Tests for the pure trophy-derivation logic in curator.psn.trophy_service."""

from __future__ import annotations

from curator.psn.models import TrophyDetail
from curator.psn.trophy_service import rarest_trophies


def _detail(trophy_id: int, rarity: float | None) -> TrophyDetail:
    return TrophyDetail(trophy_id=trophy_id, name=f"Trophy {trophy_id}", detail="", rarity=rarity)


def test_rarest_trophies_sorts_ascending_by_rarity():
    trophies = [_detail(1, 50.0), _detail(2, 5.0), _detail(3, 25.0)]

    result = rarest_trophies(trophies)

    assert [t.trophy_id for t in result] == [2, 3, 1]


def test_rarest_trophies_excludes_unknown_rarity():
    trophies = [_detail(1, 10.0), _detail(2, None), _detail(3, 5.0)]

    result = rarest_trophies(trophies)

    assert [t.trophy_id for t in result] == [3, 1]


def test_rarest_trophies_truncates_to_limit():
    trophies = [_detail(i, float(i)) for i in range(20)]

    result = rarest_trophies(trophies, limit=3)

    assert [t.trophy_id for t in result] == [0, 1, 2]


def test_rarest_trophies_empty_when_no_rated_trophies():
    assert rarest_trophies([_detail(1, None)]) == []
