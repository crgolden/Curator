"""The single SQL expression for a game's cover art, shared by every query that returns one.

Square 1:1 entitlement artwork only. ``psn_catalog_cache.cover_image_url`` holds 16:9 storefront key
art for a minority of titles, and preferring it gave the same game a differently-shaped image
depending on which query answered -- browse served widescreen, the detail page served square. Hero art
is a different kind of asset, not a better one, and needs its own treatment rather than a substitution
into a square slot.

Resolution goes through ``library_entries.title_id`` rather than ``game_concepts.concept_id``: measured
against the live catalog the title path resolves art for 1133 of 1378 games and the concept path for
1056, with every concept-path hit also covered by the title path.
"""

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

The outer query must alias ``games`` as ``g``. Scalar rather than a join so it cannot multiply rows or
skew a ``COUNT(*)`` taken over the same ``FROM`` clause.
"""
