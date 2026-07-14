"""PSN-official-tags-authoritative-over-RAWG genre reconciliation.

The legacy pipeline had no PSN-official genre signal at all (``ps_psn_ratings.py`` scraped the public PS
Store's SSR HTML for star ratings only, never genres) -- ``curator.psn.catalog_client``'s ``title_concept()``
is the new capability this closes. When PSN's own catalog data carries genre tags for a title, they're
authoritative (first-party, curated); RAWG's community-tagged genres are the fallback for titles PSN's
catalog doesn't cover.
"""

from __future__ import annotations

from curator.scoring.genre_service import pick_genre_subgenre


def reconcile_genres(
    psn_genres: list[str],
    rawg_genres: list[str],
    priorities: dict[str, int],
) -> tuple[str, str]:
    """Pick ``(genre, subgenre)``, preferring PSN's official tags over RAWG's when PSN has any.

    :param psn_genres: Genre tags from :meth:`curator.psn.catalog_client.CatalogClient.title_concept`.
    :param rawg_genres: Genre tags from RAWG's game detail response.
    :param priorities: ``name.lower() -> priority``, from the ``genres`` table (see
        :func:`~curator.scoring.genre_service.pick_genre_subgenre`).
    :returns: ``(genre, subgenre)``.
    """
    tags = psn_genres if psn_genres else rawg_genres
    return pick_genre_subgenre(tags, priorities)
