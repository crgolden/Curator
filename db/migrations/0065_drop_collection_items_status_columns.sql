-- Curator schema — migration 0065 (drop the two vestigial collection_items columns)
-- Target: PostgreSQL 17. Applied automatically by db/run_migrations.py in the deploy job.
--
-- collection_status and rotation_tier were declared in 0001 for a rotation-queue tiering that was never
-- built: no code computes either, save_run never writes them, and no reader exists. A column with a
-- CHECK and no writer is a constraint on nothing. Dropped rather than kept as documentation of an idea;
-- the idea lives in the docs.

ALTER TABLE collection_items
    DROP COLUMN collection_status,
    DROP COLUMN rotation_tier;
