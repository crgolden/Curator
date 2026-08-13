"""The single SQL expression for a game's cover art, shared by every query that returns one."""

from __future__ import annotations

SQUARE_COVER_ART_SQL = """(
                           SELECT COALESCE(es.title_image_url, es.concept_icon_url, es.game_icon_url)
                           FROM entitlement_snapshots es
                           JOIN library_entries le_art ON le_art.title_id = es.title_id
                           WHERE le_art.game_id = g.game_id
                             AND COALESCE(es.title_image_url, es.concept_icon_url, es.game_icon_url) IS NOT NULL
                           LIMIT 1
                       )"""
"""Correlated scalar subquery yielding one game's cover art, or ``NULL`` when PSN carries none.

The outer query must alias ``games`` as ``g``.
"""
