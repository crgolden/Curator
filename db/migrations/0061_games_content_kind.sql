-- Curator schema — migration 0061 (content kind for non-game catalog entries)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Streaming and media apps sit in the catalog as games. Excluding them outright is wrong because they
-- occupy real console storage, so a capacity_fill collection blind to them overruns free space; browsing
-- them as games is wrong too. content_kind records what each row is, from evidence that is never a name:
-- the PS5 entitlement package type (PSMEDIA), the mobile concept's type (APPLICATION), or the storefront
-- product's type. NULL means "not classified" and browses as a game, so shipping the column changes
-- nothing until evidence arrives.
--
-- psn_catalog_cache.concept_type keeps the mobile concept's type, which the enrichment lookup already
-- fetches and until now persisted nowhere.
--
-- The backfill classifies only what a PSMEDIA entitlement snapshot proves through the concept link;
-- nothing else is touched. The writer never overwrites a non-NULL kind with NULL.

ALTER TABLE games
    ADD COLUMN content_kind TEXT
        CHECK (content_kind IN ('game', 'media_app', 'add_on', 'demo', 'soundtrack', 'theme', 'subscription'));

ALTER TABLE psn_catalog_cache
    ADD COLUMN concept_type TEXT;

UPDATE games g
SET content_kind = 'media_app'
WHERE g.content_kind IS NULL
  AND EXISTS (
      SELECT 1
      FROM entitlement_snapshots es
      JOIN game_concepts gc ON gc.concept_id = es.concept_id
      WHERE gc.game_id = g.game_id AND es.package_type = 'PSMEDIA'
  );
