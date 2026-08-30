-- psn_enriched is the retry gate: while it is true the title is never asked of the PS Store again.
-- Rows written before the catalog client distinguished "PSN published no concept" from a resolved one
-- carry psn_enriched = true with nothing behind it, and can therefore never recover on their own.
-- Reopen exactly those: a game whose resolved title_id has no cached concept was never enriched by PSN,
-- whatever else its row holds. Clearing the flag only re-permits the fetch; it deletes no data.
-- Order matters: apply this after the Functions fix that stops a concept-less cache row being served as
-- a resolved concept, or the next run re-closes the flag on the same empty rows.

UPDATE game_enrichment
SET psn_enriched = false
WHERE psn_enriched
  AND NOT EXISTS (
      SELECT 1
      FROM psn_catalog_cache
      WHERE psn_catalog_cache.concept_id IS NOT NULL
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
