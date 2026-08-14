"""The single SQL expression for a game's cover art, shared by every query that returns one."""

from __future__ import annotations

SQUARE_COVER_ART_SQL = """(
                           SELECT COALESCE(es.title_image_url, es.concept_icon_url, es.game_icon_url)
                           FROM entitlement_snapshots es
                           JOIN entitlement_pulls ep_art ON ep_art.pull_id = es.pull_id
                           JOIN library_entries le_art ON le_art.title_id = es.title_id
                           WHERE le_art.game_id = g.game_id
                             AND COALESCE(es.title_image_url, es.concept_icon_url, es.game_icon_url) IS NOT NULL
                           ORDER BY ep_art.pulled_at DESC
                           LIMIT 1
                       )"""
"""Correlated scalar subquery yielding one game's cover art, or ``NULL`` when PSN carries none.

The outer query must alias ``games`` as ``g``.

``entitlement_snapshots`` holds one row per title *per pull*, so ``LIMIT 1`` without an order returns
whichever pull the planner reaches first and the art can change between identical requests. Ordering by
``pulled_at`` descending pins it to the most recent pull, and stays correct if snapshots are later
deduplicated to one row per entitlement.

Art is deliberately not scoped to the requesting account. ``games`` is a shared catalog, the art is public
store art, and any account that owns the title carries the same images -- scoping it per user would blank
covers for a game the caller has not entitled.
"""
