-- Curator schema — migration 0066 (drop game_enrichment.manual_score_override)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- Declared in 0001 with no route, job or migration ever writing it, and no reader in either runtime; a
-- hand override of a score would need a consent and audit surface that was never designed. NULL on every
-- row it was ever going to hold.

ALTER TABLE game_enrichment
    DROP COLUMN manual_score_override;
