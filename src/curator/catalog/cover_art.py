"""The single SQL expression for a game's cover art, shared by every query that returns one."""

from __future__ import annotations

SQUARE_COVER_ART_SQL = """(
                           SELECT COALESCE(es.title_image_url, es.concept_icon_url, es.game_icon_url)
                           FROM entitlement_snapshots es
                           JOIN library_entries le_art ON le_art.title_id = es.title_id
                           WHERE le_art.game_id = g.game_id
                             AND COALESCE(es.title_image_url, es.concept_icon_url, es.game_icon_url) IS NOT NULL
                           ORDER BY es.last_seen_at DESC
                           LIMIT 1
                       )"""
"""Correlated scalar subquery yielding one game's most recently seen cover art, or ``NULL`` when PSN
carries none. Not scoped to the requesting account.

The outer query must alias ``games`` as ``g``.

Ordering is by ``entitlement_snapshots.last_seen_at``, not by joining ``entitlement_pulls`` and ordering on
``pulled_at``: migration ``0039`` made ``pull_id`` nullable (``ON DELETE SET NULL``), so an inner join on it
silently drops the art of any snapshot whose pull has since been deleted.
"""
