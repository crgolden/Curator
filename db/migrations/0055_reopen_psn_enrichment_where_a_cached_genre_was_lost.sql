-- An enrichment pass guarded its writes on whether a provider was ATTEMPTED rather than whether it
-- SUCCEEDED, so a RAWG attempt that returned nothing wrote NULL over a genre the PS Store had supplied.
-- Those rows cannot recover on their own: psn_enriched is true, so PSN is never asked again.
-- Reopen the ones whose own cached concept still carries the genres their row has lost. The next pass
-- re-reads that cache -- no PS Store call is needed -- and recomputes the genre.
-- Apply after the Functions fix that guards on the success flags, or the same pass re-empties them.

UPDATE game_enrichment
SET psn_enriched = false
WHERE psn_enriched
  AND genre_id IS NULL
  AND EXISTS (
      SELECT 1
      FROM psn_catalog_cache
      WHERE cardinality(psn_catalog_cache.genres) > 0
        AND psn_catalog_cache.title_id = COALESCE(
            (SELECT inner_cache.title_id
             FROM psn_catalog_cache AS inner_cache
             WHERE inner_cache.game_id = game_enrichment.game_id
             ORDER BY inner_cache.title_id
             LIMIT 1),
            (SELECT library_entries.title_id
             FROM library_entries
             WHERE library_entries.game_id = game_enrichment.game_id
               AND library_entries.title_id IS NOT NULL
             ORDER BY library_entries.title_id
             LIMIT 1)));
