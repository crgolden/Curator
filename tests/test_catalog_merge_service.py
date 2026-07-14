"""Tests for merge_by_product_id_and_name(), isolated from the full canonicalize() pipeline."""

from __future__ import annotations

from curator.catalog.canonicalization_service import GroupedEntry
from curator.catalog.merge_service import merge_by_product_id_and_name


def _entry(name, product_id, concept_id, package_type="PS4GD"):
    return GroupedEntry(
        name=name,
        package_type=package_type,
        concept_id=concept_id,
        product_id=product_id,
        entitlement_id=f"ent-{concept_id}",
    )


def test_merges_two_groups_sharing_product_id_and_name():
    groups = {
        "c1": [_entry("Same Game", "PID-1", "c1", package_type="PS4GD")],
        "c2": [_entry("Same Game", "PID-1", "c2", package_type="PSGD")],
    }

    merged = merge_by_product_id_and_name(groups)

    assert len(merged) == 1
    (entries,) = merged.values()
    assert {e.concept_id for e in entries} == {"c1", "c2"}


def test_merge_is_case_insensitive_on_name():
    groups = {
        "c1": [_entry("Same Game", "PID-1", "c1")],
        "c2": [_entry("SAME GAME", "PID-1", "c2")],
    }

    merged = merge_by_product_id_and_name(groups)

    assert len(merged) == 1


def test_does_not_merge_when_names_disagree():
    # Sony's raw data has been observed pointing two genuinely different games at the same wrong
    # product id -- a bare product-id merge would incorrectly conflate them.
    groups = {
        "c1": [_entry("BioShock: The Collection", "PID-SHARED", "c1")],
        "c2": [_entry("Bioshock Infinite: The Complete Edition", "PID-SHARED", "c2")],
    }

    merged = merge_by_product_id_and_name(groups)

    assert len(merged) == 2
    assert set(merged.keys()) == {"c1", "c2"}


def test_does_not_merge_when_only_one_group_has_the_product_id():
    groups = {
        "c1": [_entry("Solo Game", "PID-ONLY", "c1")],
        "c2": [_entry("Other Game", "", "c2")],
    }

    merged = merge_by_product_id_and_name(groups)

    assert len(merged) == 2


def test_groups_without_a_product_id_pass_through_unmerged():
    groups = {"c1": [_entry("No Product Id", "", "c1")]}

    merged = merge_by_product_id_and_name(groups)

    assert merged == groups


def test_three_way_merge_on_shared_product_id_and_name():
    groups = {
        "c1": [_entry("Triple Game", "PID-3", "c1")],
        "c2": [_entry("Triple Game", "PID-3", "c2")],
        "c3": [_entry("Triple Game", "PID-3", "c3")],
    }

    merged = merge_by_product_id_and_name(groups)

    assert len(merged) == 1
    (entries,) = merged.values()
    assert len(entries) == 3
