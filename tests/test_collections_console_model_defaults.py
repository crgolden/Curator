"""Tests for default_capacity_gb(), the POST /consoles auto-assign-a-default-size lookup (WP3)."""

from __future__ import annotations

from curator.collections.console_model_defaults import default_capacity_gb


def test_matches_a_known_ps5_model():
    capacity, matched = default_capacity_gb("PS5", "PS5 Digital Edition")

    assert capacity == 667.0
    assert matched is True


def test_matches_a_known_ps4_model():
    capacity, matched = default_capacity_gb("PS4", "PS4 Slim 1TB")

    assert capacity == 850.0
    assert matched is True


def test_falls_back_to_the_platform_default_when_model_is_none():
    capacity, matched = default_capacity_gb("PS5", None)

    assert capacity == 667.0
    assert matched is False


def test_falls_back_to_the_platform_default_when_model_is_unrecognized():
    capacity, matched = default_capacity_gb("PS4", "PS4 Mystery Edition")

    assert capacity == 430.0
    assert matched is False


def test_falls_back_to_a_generic_default_for_an_unrecognized_platform():
    capacity, matched = default_capacity_gb("Switch", None)

    assert capacity == 500.0
    assert matched is False
