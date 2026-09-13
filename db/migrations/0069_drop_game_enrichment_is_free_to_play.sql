-- Curator schema — migration 0069 (drop game_enrichment.is_free_to_play)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Read by the collection ranking on every run and written by nothing, so the free-to-play adjustment never
-- fired. The storefront's own price node (0062, psn_catalog_cache.price_is_free) is the honest source and
-- the ranking now reads that; a second, provider-shaped column for the same fact would drift from it.

ALTER TABLE game_enrichment
    DROP COLUMN is_free_to_play;
