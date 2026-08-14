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
"""Correlated scalar subquery yielding one game's cover art from its most recent entitlement pull, or
``NULL`` when PSN carries none. Not scoped to the requesting account.

The outer query must alias ``games`` as ``g``.
"""
