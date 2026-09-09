"""The single SQL expression for a game's cover art, shared by every query that returns one."""

from __future__ import annotations

SQUARE_COVER_ART_SQL = """COALESCE((
                           SELECT COALESCE(es.title_image_url, es.concept_icon_url, es.game_icon_url)
                           FROM entitlement_snapshots es
                           JOIN library_entries le_art ON le_art.title_id = es.title_id
                           WHERE le_art.game_id = g.game_id
                             AND COALESCE(es.title_image_url, es.concept_icon_url, es.game_icon_url) IS NOT NULL
                           ORDER BY es.last_seen_at DESC
                           LIMIT 1
                       ), g.store_cover_image_url)"""
"""Correlated scalar subquery yielding one game's most recently seen cover art, falling back to the cover
a PlayStation Store search supplied at admission, or ``NULL`` when neither exists. Not scoped to the
requesting account.

**The entitlement artwork wins, and the order is the point.** Store art is a fallback for games nobody
holds an entitlement for -- a disc added by hand -- so a game that renders art today renders the same art
after ``0057``. Reversing the order would silently restyle the whole catalogue from a different source.

The outer query must alias ``games`` as ``g``.

Ordering is by ``entitlement_snapshots.last_seen_at``, not by joining ``entitlement_pulls`` and ordering on
``pulled_at``: migration ``0039`` made ``pull_id`` nullable (``ON DELETE SET NULL``), so an inner join on it
silently drops the art of any snapshot whose pull has since been deleted.
"""
