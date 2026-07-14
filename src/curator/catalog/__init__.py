"""Curator's shared-catalog canonicalization slice.

Ported from ``Tools\\PlayStation\\ps_curate.py``: deduplicates raw per-entitlement rows into one row per
real game (across editions, platforms, and Sony's own occasional concept-id/product-id inconsistencies),
decoupled from that script's Excel-workbook I/O and its four hardcoded config dicts, which now live in
Curator's curation-rule tables (``exclusion_rules``, ``franchise_rules``, ``edition_ranks``,
``game_name_overrides``) and are passed into the pure functions here by the caller.
"""

from __future__ import annotations
