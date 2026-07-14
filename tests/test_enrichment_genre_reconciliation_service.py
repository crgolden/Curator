"""Tests for reconcile_genres(): PSN-official tags authoritative over RAWG when present."""

from __future__ import annotations

from curator.enrichment.genre_reconciliation_service import reconcile_genres

_PRIORITIES = {"sports": 0, "racing": 1, "simulation": 2, "family": 3}


def test_psn_genres_used_when_present():
    result = reconcile_genres(["Simulation", "Sports"], ["Racing"], _PRIORITIES)
    assert result == ("Sports", "Simulation")


def test_falls_back_to_rawg_when_psn_has_no_genres():
    result = reconcile_genres([], ["Family", "Simulation"], _PRIORITIES)
    assert result == ("Simulation", "Family")


def test_both_empty_returns_empty_strings():
    assert reconcile_genres([], [], _PRIORITIES) == ("", "")
