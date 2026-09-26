"""Tests for default_capacity_gb(), the POST /consoles auto-assign-a-default-size lookup (WP3)."""

from __future__ import annotations

import pytest

from curator.collections.console_model_defaults import (
    MODEL_CAPACITY_GB,
    PLATFORM_FALLBACK_GB,
    UNKNOWN_PLATFORM_FALLBACK_GB,
    default_capacity_gb,
)
from test_values import lowercase_token


@pytest.mark.parametrize("model", list(MODEL_CAPACITY_GB))
def test_a_known_model_resolves_to_its_own_published_figure(model):
    capacity, matched = default_capacity_gb(lowercase_token(), model)

    assert capacity == MODEL_CAPACITY_GB[model]
    assert matched is True


@pytest.mark.parametrize("platform", list(PLATFORM_FALLBACK_GB))
def test_falls_back_to_the_platform_default_when_model_is_none(platform):
    capacity, matched = default_capacity_gb(platform, None)

    assert capacity == PLATFORM_FALLBACK_GB[platform]
    assert matched is False


@pytest.mark.parametrize("platform", list(PLATFORM_FALLBACK_GB))
def test_falls_back_to_the_platform_default_when_model_is_unrecognized(platform):
    capacity, matched = default_capacity_gb(platform, lowercase_token())

    assert capacity == PLATFORM_FALLBACK_GB[platform]
    assert matched is False


def test_falls_back_to_a_generic_default_for_an_unrecognized_platform():
    capacity, matched = default_capacity_gb(lowercase_token(), None)

    assert capacity == UNKNOWN_PLATFORM_FALLBACK_GB
    assert matched is False
