"""Curator's shared-catalog canonicalization slice.

Deduplicates raw per-entitlement rows into one row per real game (across editions, platforms, and Sony's
own occasional concept-id/product-id inconsistencies). The curation rules driving that live in
``exclusion_rules``, ``franchise_rules``, ``edition_ranks`` and ``game_name_overrides``, and are passed
into the pure functions here by the caller.
"""

from __future__ import annotations
